import socketio
import threading
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from livedatacollect4 import IBapi

from datetime import datetime

import threading
import time
import keyboard
import os
import Start_config as config

from SocketIO_Client import FrontEndClient

tickers = config.tickers
count = 0


def run_loop():
	app.run()

app = IBapi()

websock = FrontEndClient(app)
wst = threading.Thread(target=websock.connectwebsocket)

# wst = threading.Thread(target=connectwebsocket)
wst.daemon = True
wst.start()


app.connect('127.0.0.1', 7497, 123)

time.sleep(1)

#Start the socket in a thread
api_thread = threading.Thread(target=app.run,daemon=True)
api_thread.start()

app.startData(websock.sio,tickers,[],1,8) # Backtesting


print("___________________________________________________________")
print("--------------Press 'CTRL' to Close Program----------------")
print("___________________________________________________________")

keyboard.wait('Ctrl')

print("___________________________________________________________")
print("------------------Closing Program...-----------------------")
print("___________________________________________________________")

app.disconnect()

time.sleep(2)

print("Data Collection Closed")