import socketio
import threading
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from MainStoinker.NeatTools.decorators import singleton

from MainStoinker.DataCollection.apiApi import IBapi
from datetime import datetime
from enum import Enum

import threading
import time
import keyboard
import os
import MainStoinker.MainStuff.Start_config as config
import main_utils as utils
import logging

from MainStoinker.Util.SocketIO_Client import FrontEndClient

eventDict = {}

for name in config.loggers:
    utils.create_logger(name)
logger = logging.getLogger("dum_dum")
count = 0

logger.info("___________________________________________________________")
logger.info("---------------------Starting Program----------------------")
logger.info("___________________________________________________________\n")
AlgoList = utils.algo_config_parse()
print(AlgoList) 


app = IBapi()




websock = FrontEndClient()
if config.FrontEndDisplay:
    wst = threading.Thread(target=websock.connect_websocket,daemon=True)
    wst.start()

time.sleep(1)
app.connect('127.0.0.1', 7497, 123)


while(not app.isConnected()):
    print("Order Status: " + str(app.isConnected()))
    time.sleep(.5)
time.sleep(1)
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

print("startup read orders")
print(app.readOrders())

app.startData(config.tickers,AlgoList,2,eventDict,config.Duration) # Backtesting

if not config.LiveData:
    eventDict[0].wait()
    # rint("Event called for backtesty")
    utils.backtesting_data_blast()
    eventDict[0].clear()



logger.info("___________________________________________________________")
logger.info("--------------Press 'DEL' to Close Program----------------")
logger.info("___________________________________________________________\n")

keyboard.wait('Delete')

config.updating = 0

logger.info("___________________________________________________________")
logger.info("------------------Closing Program...-----------------------")
logger.info("___________________________________________________________\n")

app.disconnect()

time.sleep(2)

print("TWS Collection Closed")




