import json
import os
import importlib
import Start_config
# print(os.getcwd())

def AlgoConfigParse():
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

    for algos in parsed_json:
        # print(algos)
        algoName = list(algos.keys())[0]
        algolist.append(algoName)
        for algoConfigData in algos[algoName]:
            print(algoConfigData['ticker'])
            algoObjectList.append(AlgoStarter(algoName,algoConfigData))
            if algoConfigData['ticker'] in tickerlist:
                print("ticker already in tickerlist")

            else:
                tickerlist.append(algoConfigData['ticker'])

            algoCount += 1

    print("Total Algos: " + str(algoCount))
    # print(algolist) # list of all the unique algos
    # print(tickerlist) # list of all the unique tickers
    # print(parsed_json) # all the data from json file
    Start_config.tickers = tickerlist
    return algoObjectList


def AlgoStarter(algo, data):
    filename = str(algo) + "_Algo"
    print("Opening " + filename)
    AlgoClass = getattr(importlib.import_module(filename),'Algo')
    return AlgoClass(data)


def ConfigSend(socket):
    file = open('./MainStoinker/DataCollection/Test/Algo_config.json')

    try:
        parsed_json = json.load(file)
    except Exception as e:
        print("Got the following exception: " + str(e))

    file.close()
    print("Sending Algo Config")
    print(parsed_json)
    try:
        socket.emit('config_send', parsed_json)
    except:
        print("Cant send Config, /Not connected to front end")


# AlgoConfigParse()