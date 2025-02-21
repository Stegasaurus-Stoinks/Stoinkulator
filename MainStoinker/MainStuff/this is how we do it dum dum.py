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

# check for config errors before doing anything else
utils.check_valid_config()

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
print(config.algos)
print(config.tickers) 

# if not config.offline:
app = IBapi()



    
websock = FrontEndClient()
if config.FrontEndDisplay:
    wst = threading.Thread(target=websock.connect_websocket,daemon=True)
    wst.start()
    time.sleep(1)


if not config.offline:

    app.connect('127.0.0.1', 7497, 123)

    while(not app.isConnected()):
        print("TWS Connection: ", str(app.isConnected()))
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

    app.startData(config.tickers,AlgoList,2,eventDict,config.Duration) # Backtesting

else: #if offline load offline data
    eventDict[0] = threading.Event()
    time.sleep(.5)
    print("____________________________________________")
    print("Setting up offline thread...")
    print("Loading offline data for tickers from CSV...")
    print("")
    app.startData(config.tickers,AlgoList,2,eventDict,config.Duration) # Backtesting


# backtesting loop
if not config.LiveData:
    eventDict[0].wait()

    # collect offline data if configured to do so
    if(config.collectofflinedata):
        print(app.simulatedDatadict)
        for ticker in config.tickers:
            tickerdf = app.simulatedDatadict[ticker]
            tickerdf.to_csv("./OfflineData/_" + str(config.tickers[ticker].name) + "_offlinedata_")
    
    utils.backtesting_data_blast()
    eventDict[0].clear()



logger.info("___________________________________________________________")
logger.info("--------------Press 'DEL' to Close Program----------------")
logger.info("___________________________________________________________\n")

keyboard.wait('Delete')

utils.get_algo_data()

config.updating = 0

logger.info("___________________________________________________________")
logger.info("------------------Closing Program...-----------------------")
logger.info("___________________________________________________________\n")

app.disconnect()

time.sleep(2)

print("TWS Collection Closed")




