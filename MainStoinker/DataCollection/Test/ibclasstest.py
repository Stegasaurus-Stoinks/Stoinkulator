import socketio
import threading
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from livedatacollect4 import IBapi

from datetime import datetime

from enum import Enum
 
from algotesty import AlgoConfigParse
from EMACrossing_Algo import Algo as EMAAlgo

import threading
import time
import keyboard
import os
import Start_config as config

from SocketIO_Client import FrontEndClient

from testyclass import testibkr


count = 0

AlgoList = AlgoConfigParse()
print(AlgoList)

def run_loop():
	app.run()

app = IBapi()

websock = FrontEndClient(app)
if config.FrontEndDisplay:
    wst = threading.Thread(target=websock.connectwebsocket,daemon=True)
    wst.start()

time.sleep(1)
app.connect('127.0.0.1', 7497, 123)
while(not app.isConnected):
    time.sleep(.5)
print("TWS Connected")

api_thread = threading.Thread(target=app.run,daemon=True)
api_thread.start()
# print("before run")
# app.run()
# print("after run")

#verify connection has read/write capabilities
if not app.getNextOrderID():
    print("Something wrong with connection (no response from TWS)")
    print("Shutting Down...")
    time.sleep(1)
    app.disconnect
    time.sleep(1)
    exit()

print("Read positions in ibclasstest:")
print(app.readPositions())

# app.startData(websock.sio,config.tickers,AlgoList,2,config.Duration) # Backtesting
app.testingtrading(AlgoList)





print("___________________________________________________________")
print("--------------Press 'CTRL' to Close Program----------------")
print("___________________________________________________________")
print("")

keyboard.wait('Ctrl')

config.updating = 0

print("___________________________________________________________")
print("------------------Closing Program...-----------------------")
print("___________________________________________________________")

app.disconnect()

time.sleep(2)

print("TWS Collection Closed")