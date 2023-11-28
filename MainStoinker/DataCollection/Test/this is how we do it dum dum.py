import socketio
import threading
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from MainStoinker.NeatTools.decorators import singleton

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
import main_utils as utils

from SocketIO_Client import FrontEndClient

eventDict = {}


count = 0

AlgoList = AlgoConfigParse()
print(AlgoList)


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


if config.LiveData:
    for ticker in config.tickers.values():
        eventDict[ticker.index] = threading.Event()

    for index, event in eventDict.items():
        event_thread = threading.Thread(target=utils.event_loop, args=(event, index,), daemon=True, name=config.tickers[index].name)
        event_thread.start()
else:
    eventDict[0] = threading.Event()


#verify connection has read/write capabilities
if not app.getNextOrderID():
    print("Something wrong with connection (no response from TWS)")
    print("Shutting Down...")
    time.sleep(1)
    app.disconnect
    time.sleep(1)
    exit()

print("startup read positions")
print(app.readPositions())

app.startData(websock.sio,config.tickers,AlgoList,2,eventDict,config.Duration) # Backtesting

if not config.LiveData:
    eventDict[0].wait()
    print("Event called for backtesty")
    utils.backtesting_data_blast()
    eventDict[0].clear()



print("___________________________________________________________")
print("--------------Press 'CTRL' to Close Program----------------")
print("___________________________________________________________")
print("")

keyboard.wait('Ctrl')
print(app.readpositions)

config.updating = 0

print("___________________________________________________________")
print("------------------Closing Program...-----------------------")
print("___________________________________________________________")

app.disconnect()

time.sleep(2)

print("TWS Collection Closed")




