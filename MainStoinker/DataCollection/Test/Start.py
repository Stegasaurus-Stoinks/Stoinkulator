import Start_config

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

import threading
import time
import keyboard
import os
from livedatacollect3 import IBapi

def run_loop():
	app.run()

app = IBapi()
app.connect('127.0.0.1', 7497, 123)
# app.run()

#Start the socket in a thread
api_thread = threading.Thread(target=run_loop,daemon=True)
api_thread.start()

time.sleep(1) #Sleep interval to allow time for connection to server

tickers = ["AAPL"]#,"GME","MSFT"]
# tickers = ["ETH"]

app.startData(tickers,2,Start_config.LiveData,10)

# ^^^ All Code Stuff Goes Above This Line ^^^

print("___________________________________________________________")
print("--------------Press 'CTRL' to Close Program----------------")
print("___________________________________________________________")

keyboard.wait('Ctrl')

print("___________________________________________________________")
print("------------------Closing Program...-----------------------")
print("___________________________________________________________")

app.disconnect()
mydir = os.path.join(os.path.dirname(__file__), './Collected_Data/')
for f in os.listdir(mydir):
    os.remove(os.path.join(mydir, f))