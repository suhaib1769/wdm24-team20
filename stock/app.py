import logging
import os
import atexit
import uuid

import redis

from msgspec import msgpack, Struct
# from flask import Flask, jsonify, abort, Response

import asyncio
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer, AIOKafkaClient
from quart import Quart, request, jsonify, abort, Response

DB_ERROR_STR = "DB error"
KAFKA_SERVER = os.environ.get('KAFKA_SERVER', 'kafka:9092')


app = Quart("stock-service")

db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
                              port=int(os.environ['REDIS_PORT']),
                              password=os.environ['REDIS_PASSWORD'],
                              db=int(os.environ['REDIS_DB']))


def close_db_connection():
    db.close()


atexit.register(close_db_connection)


class StockValue(Struct):
    stock: int
    price: int

producer = None
consumer = None

async def init_kafka_producer():
    global producer
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_SERVER,
        enable_idempotence=True,
        transactional_id="stock-service-transactional-id"
    )
    await producer.start()

async def init_kafka_consumer():
    global consumer
    consumer = AIOKafkaConsumer(
        'stock_request',
        bootstrap_servers='kafka:9092',
        group_id="my-group2",
        enable_auto_commit=False,
        isolation_level="read_committed")
    # Get cluster layout and join group `my-group`
    await consumer.start()

async def stop_kafka_producer():
    await producer.stop()

async def consume():
    app.logger.info("Entered consume")
   
    try:
        # Consume messages
        app.logger.info("Entered consume try")
        async for msg in consumer:
            app.logger.info("msg in consumer")
            stock_data = msgpack.decode(msg.value)
            action = stock_data['action']
            if action == 'find':
                app.logger.info("msg in action find consumer")
                item_id = stock_data["item_id"]
                app.logger.info("calling find item")
                await find_item(item_id)

    finally:
        # Will leave consumer group; perform autocommit if enabled.
        await consumer.stop()


def get_item_from_db(item_id: str) -> StockValue | None:
    # get serialized data
    app.logger.info("Entered get item from db")
    try:
        entry: bytes = db.get(item_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    # deserialize data if it exists else return null
    entry: StockValue | None = msgpack.decode(entry, type=StockValue) if entry else None
    app.logger.info(entry)
    return entry
    


@app.post('/item/create/<price>')
def create_item(price: int):
    key = str(uuid.uuid4())
    app.logger.info(f"Item: {key} created")
    value = msgpack.encode(StockValue(stock=0, price=int(price)))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(StockValue(stock=starting_stock, price=item_price))
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


# @app.get('/find/<item_id>')
# def find_item(item_id: str):
#     item_entry: StockValue = get_item_from_db(item_id)
#     return jsonify(
#         {
#             "stock": item_entry.stock,
#             "price": item_entry.price
#         }
#     )

async def find_item(item_id: str):
    app.logger.info("Entered stock find item")
    item_entry = get_item_from_db(item_id)
    if item_entry is None:
        # if item does not exist in the database; abort
        # abort(400, f"Item: {item_id} not found!")
        return {'status': '400', 'msg': f"Item: {item_id} not found!"}
    app.logger.info(item_entry)
    item =   {
            "stock": item_entry.stock,
            "price": item_entry.price
        }
    # item_entry = {'status': '200', 'msg': item} 
    app.logger.info(item)

    try:
        message = {'action': 'find', 'msg': item, "status": '200'}
        app.logger.info("Entered try find item")
        async with producer.transaction():
            app.logger.info("Before send and wait")
            app.logger.info(message)
            await producer.send_and_wait('stock_response', value=msgpack.encode(message))
            app.logger.info("After send and wait")
            
    finally:
        await stop_kafka_producer()
    
   


@app.post('/add/<item_id>/<amount>')
def add_stock(item_id: str, amount: int):
    item_entry: StockValue = get_item_from_db(item_id)
    # update stock, serialize and update database
    app.logger.info(str(item_entry))
    item_entry.stock += int(amount)
    try:
        db.set(item_id, msgpack.encode(item_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {item_entry.stock}", status=200)


@app.post('/subtract/<item_id>/<amount>')
def remove_stock(item_id: str, amount: int):
    item_entry: StockValue = get_item_from_db(item_id)
    # update stock, serialize and update database
    item_entry.stock -= int(amount)
    app.logger.debug(f"Item: {item_id} stock updated to: {item_entry.stock}")
    if item_entry.stock < 0:
        abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
    try:
        db.set(item_id, msgpack.encode(item_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {item_entry.stock}", status=200)

async def main():
    await init_kafka_producer()
    await init_kafka_consumer()
    # Keep the service running
    kafka_task = asyncio.gather(consume())
    # app_task = asyncio.create_task(app.run_task(host='0.0.0.0', port=8000))
    # await asyncio.gather(kafka_task, app_task)
    await asyncio.Event().wait() 


@app.before_serving
async def run_main():
    await main()

@app.after_serving
async def shutdown():
    await stop_kafka_producer()
    await stop_kafka_consumer()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
    try:
        asyncio.run(main()) 
    finally:
        asyncio.run(stop_kafka_producer())
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
