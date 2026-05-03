from app.brokers.tradovate import TradovateClient

client = TradovateClient(
    username="YOUR_USERNAME",
    password="YOUR_PASSWORD",
    cid="YOUR_CID",
    sec="YOUR_SEC"
)

print("Logging in...")
login_result = client.login("demo")
print(login_result)

print("\nFetching accounts...")
accounts_result = client.get_accounts("demo")
print(accounts_result)