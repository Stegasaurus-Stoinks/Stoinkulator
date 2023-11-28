import json
import os
import importlib
import Start_config as config
from ticker import Ticker
# print(os.getcwd())

def AlgoConfigParse():
    tickerDict = {}
    configTickerDict = {}

    file = open('./MainStoinker/DataCollection/Test/Algo_config.json')

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

            ticker = tickerDict.get(tickerName) 
            
            if ticker is None:
                # create ticker object
                ticker = Ticker(tickerName, config.tickerIndex)
                tickerDict[tickerName] = ticker
                config.tickerIndex += 1
                configTickerDict[ticker.index] = ticker
            
            
            algo = AlgoStarter(algoName,algoConfigData)
            ticker.register_algo(algo)
            algoObjectList.append(algo)

            algoCount += 1

    print("Total Algos: " + str(algoCount))
    print(configTickerDict)
    # print(algolist) # list of all the unique algos
    # print(tickerlist) # list of all the unique tickers
    # print(parsed_json) # all the data from json file
    config.tickers = configTickerDict
    config.algos = algoObjectList

    return algoObjectList


def AlgoStarter(algo, data):
    filename = str(algo) + "_Algo"
    print("Opening " + filename)
    AlgoClass = getattr(importlib.import_module(filename),'Algo')
    return AlgoClass(data)




# AlgoConfigParse()