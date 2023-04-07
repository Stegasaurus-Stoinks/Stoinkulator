import Start_config
import livedatacollect2

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import threading
import time
from livedatacollect3 import IBapi

def run_loop():
	app.run()

app = IBapi()
app.connect('127.0.0.1', 7497, 123)
# app.run()

#Start the socket in a thread
api_thread = threading.Thread(target=run_loop, daemon=True)
api_thread.start()

time.sleep(1) #Sleep interval to allow time for connection to server

#Create contract object
apple_contract = Contract()
apple_contract.symbol = 'EUR.USD'
apple_contract.secType = 'CASH'
apple_contract.exchange = ''
apple_contract.currency = 'USD'

tickers = ["AAPL","GME","MSFT"]
# tickers = ["ETH"]

print("test")
app.startData(tickers)
print("test")


# time.sleep(20) #Sleep interval to allow time for incoming price data
# app.disconnect()

