from MainStoinker.MainStuff import Globals
from MainStoinker.MainStuff.Globals import config
from MainStoinker.Util.SocketIO_Client import frontend_client
import simplejson
from MainStoinker.DataCollection.apiApi import ibapi
from datetime import datetime
import time
import importlib
import pandas as pd
import json
from MainStoinker.TradeTools.ticker import Ticker
import logging
import pandas as pd



def event_loop(event, index):
    while(not event.is_set()):
        event.wait()
        # print("Event called for " + Start_config.tickers[index].name)
        Globals.tickers[index].update_algos()
        event.clear()


def algo_config_parse():
    tickerDict = {}
    configTickerDict = {}

    file = open('./MainStoinker/MainStuff/Algo_config.json')

    tickerlist = []
    algolist = []
    algoObjectList = []
    try:
        parsed_json = json.load(file)
    except Exception as e:
        print("Got the following exception: " + str(e))

    file.close()

    algoCount = 0
    # print(parsed_json)
    for algo in parsed_json:
        # print(algo)
        algoName = algo['ID']
        # print(algoName)
        algolist.append(algoName)
        for algoConfigData in algo['data']:
            tickerName = algoConfigData['ticker']
            # print(algoConfigData['ticker'])

            # TODO: replace ticketDict name check with a check that iterates over configTickerDict, and checks for both name and timeFrame
            # TODO: Add timeframe variable to algo_config.json
            # TODO: After finishing above, change code to dynamically request data based off timeFrame and remove hardcoded values
            ticker = tickerDict.get(tickerName) 
            
            if ticker is None:
                # create ticker object
                ticker = Ticker(tickerName, Globals.tickerIndex)
                tickerDict[tickerName] = ticker
                Globals.tickerIndex += 1
                configTickerDict[ticker.index] = ticker
            
            
            algo = algo_starter(algoName,algoConfigData)
            ticker.register_algo(algo)
            algoObjectList.append(algo)

            algoCount += 1

    print("Total Algos: " + str(algoCount))
    print(configTickerDict)
    # print(algolist) # list of all the unique algos
    # print(tickerlist) # list of all the unique tickers
    # print(parsed_json) # all the data from json file
    Globals.tickers = configTickerDict
    Globals.algos = algoObjectList

    return algoObjectList


def algo_starter(algo, data):
    filename = "." + str(algo) + "_Algo"
    print("Opening " + filename)
    AlgoClass = getattr(importlib.import_module(filename,"MainStoinker.Algos."+str(algo)),'Algo')
    return AlgoClass(data)



def backtesting_data_blast():
    print("---Simulated Live Data Starting Now...---")
    simulatedDatadict = ibapi.simulatedDatadict

    #TODO: MOVE FRONT END STUFF SOMEWHERE ELSE
    if config.FrontEndDisplay:
        tickerfulldata = []
        frontend_client.Config_send()
        for i in range(len(Globals.tickers)):
            Fulldata = get_data_json(index = i)
            tickerfulldata.append({'ticker': Globals.tickers[i].name, 'data':Fulldata})
        print("Sending Fulldata")
        try:
            # self.socket.emit('update_send',{)
            payload = {"tickerdata":tickerfulldata}
            payload = simplejson.dumps(payload, ignore_nan=True)
            # print(payload)
            frontend_client.sio.emit('data_send',payload)
        except Exception as e:
            print(e)


    startpoint = Globals.tickers[0].data.shape[0]
    numpoints = simulatedDatadict[Globals.tickers[0].index].shape[0] - startpoint


    starttime = datetime.now()
    #for every point collected during backtesting
    for k in range(numpoints):
        #for all tickers in list
        for ticker in Globals.tickers.values():
            while(not Globals.updating):
                time.sleep(1)
            try:
                entry = simulatedDatadict[ticker.index].iloc[startpoint+k]
                ticker.append(entry)
            except Exception as e:
                print("hello this is justin telling you that the point you were looking for doesnt actually exist!")
                print("also paul says that we're in main_utils.backtesting_data_BLAST")
                print(e)
                time.sleep(10)
        
        # loop through tickers and update algos
        # we do this separate to get all data for minute first and then analyze
        for ticker in Globals.tickers.values():
            ticker.update_algos()


        time.sleep(config.TimeDelayPerPoint)
        print(entry.iloc[0])

    # Done with the Backtesting loop here
    get_algo_data()

    endtime = datetime.now()
    duration = endtime-starttime
    print("Backtesting "+str(numpoints)+" Points is Done!")
    print("Duration: " + str(duration))
    print("___________________________________________________________")
    print("--------------Press 'CTRL' to Close Program----------------")
    print("___________________________________________________________")

def get_data_json(index):
    result = Globals.tickers[index].data.to_json(orient="records")
    # print(result)
    return(result)

# Grabs all algo data from all tickers and makes big ole df and sends it to a file
def get_algo_data():
    temparray = []
    for ticker in Globals.tickers.values():
            temparray.append(ticker.save_algos())
            pd.concat(temparray).to_csv('algo.csv')





def create_logger(name, log_level=config.log_level):
    # create logger
    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    # create console handler and set level to debug
    fh = logging.FileHandler('loggy.log')
    fh.setLevel(log_level)
    ch = logging.StreamHandler()
    ch.setLevel(log_level)

    # create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # add formatter to ch
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    # add ch to logger
    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger


def check_valid_config():
    error = ""
    errordetected = 0
    if config.offline:
        if config.collectofflinedata:
            error = error + " | Cant collect offline data when offline (collectofflinedata = True)"
            errordetected = 1
        if config.LiveData:
            error = error + " | Conflicting Data Types: LiveData = True and Offline = True"
            errordetected = 1
        if config.LiveTrading:
            error = error + " | Cant LiveTrading when offline = True"
            errordetected = 1

        # to be continued as we find more cases of conflicting config settings

        if errordetected == 1:
            print("Config Error Detected")
            print("Canceling Program, Please check config settings")
            print("Error message: " + error)
            quit()


