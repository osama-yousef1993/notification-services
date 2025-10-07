"""Basic connection example.
"""

import redis

class RD:
    def __init__(self):
        self.r = redis.Redis(
            host='redis-18646.c10.us-east-1-3.ec2.redns.redis-cloud.com',
            port=18646,
            decode_responses=True,
            username="default",
            password="nr5wH8AaJg5l32DqZFJVthkVoqvoGHDo",
        )
    def add_redis(self):
        self.r.set('foo', 'bar')
        return True

    def get_redis(self):
        return self.r.get('foo')

