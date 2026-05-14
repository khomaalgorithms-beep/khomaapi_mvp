from app.brokers.tradovate import TradovateClient

client = TradovateClient(
    username="DmytriiKhoma",
    password="Dimaoffkh25112008@",
    cid="13281",
    sec="3c7f3c53-0377-45f2-b3f2-04eda8b5a588"
)

print("Logging in...")

login_result = client.login("live")
print(login_result)

if login_result.get("ok"):

    print("\nFetching accounts...")

    accounts_result = client.get_accounts("live")
    print(accounts_result)

    if accounts_result.get("ok"):

        account_id = accounts_result["accounts"][0]["id"]

        print("\nPlacing TEST order...")

        order_result = client.place_order(
            account_id=account_id,
            symbol="MNQM6",
            side="buy",
            qty=1,
            environment="live"
        )

        print(order_result)

else:

    print("Login failed.")

