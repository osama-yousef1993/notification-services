from src.utils.redis import RD

def main():
    rd = RD()
    rd.add_redis()
    value = rd.get_redis()
    print(f"Value from Redis: {value}")


if __name__ == "__main__":
    main()