from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import BarData
from ibapi.common import *
from csv import writer
from datetime import datetime
from datetime import timedelta
import algotesty
import simplejson

import Start_config as config

import numpy as np
import pandas as pd

import threading
import time

import json

def createStockContact(ticker: str):
    contract = Contract()
    contract.secType = "STK"
    contract.symbol = ticker
    contract.currency = "USD"
    contract.exchange = "SMART"

    return contract


class IBapi(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        
                
    def tickPrice(self, reqId, tickType, price, attrib):
        if tickType == 2 and reqId == 1:
            print('The current ask price is: ', price)

    def nextValidId(self, orderId: int):
        print("Setting nextValidOrderId: %d", orderId)
        self.nextValidOrderId = orderId
        # self.startData("AAPL")

    def historicalData(self, reqId: int, bar: BarData):
        # print("HistoricalData. ReqId:", reqId, "BarData.", bar)
        candleData = [datetime.fromtimestamp(int(bar.date)),int(bar.date), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]

        if(config.LiveData):
            self.datadict[reqId] = self.datadict[reqId]._append([candleData], ignore_index=True)
        else:
            self.simulatedDatadict[reqId] = self.simulatedDatadict[reqId].append([candleData], ignore_index=True)


    def historicalDataEnd(self, reqId: int, start: str, end: str):
        # print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        
        
        if(config.LiveData):
            self.datadict[reqId].columns=['date','time','open','high','low','close','volume','average']
            print(self.datadict[reqId])
            print("All Historical Data Collected: Live Data Starting Now...")
            # nothing else is needed here because historical data was set to keep live data
            if config.FrontEndDisplay:
                Fulldata = self.getDataJson(index = 0)
                # self.socket.emit('data_send', 'AAPL', Fulldata)
                # self.socket.emit('data_send', {'ticker': 'AAPL', 'data':Fulldata})
                
                   
        else:
            self.simulatedDatadict[reqId].columns=['date','time','open','high','low','close','volume','average']
            print("Historical Data Collected for " + self.tickers[reqId])
            self.datacollectednum += 1
            # print(self.simulatedDatadict[reqId])
            # Create datadict data frame here
            # ____________________________________________________________________________________
            firstDate = self.simulatedDatadict[reqId].at[0,'date']
            startDate = firstDate + timedelta(days=self.warmup)
            print("Warmup Start Date: " + str(firstDate))
            print("Warmup End Date: " + str(startDate))
            self.datadict[reqId] = self.simulatedDatadict[reqId].loc[(self.simulatedDatadict[reqId]['date'] < startDate)]
            self.datadict[reqId].columns=['date','time', 'open','high','low','close','volume','average']
            # print(self.datadict[reqId])

            if self.datacollectednum >= len(self.tickers): #all historical data collected
                print("------All Historical Data Collected------")
                print("---Simulated Live Data Starting Now...---")
                self.backtestingDataUpdate()




    def historicalDataUpdate(self, reqId: int, bar: BarData):           # Live Data Updates
        tickerdata = []
        algodata = []

        data = {'date':[datetime.fromtimestamp(int(bar.date))],
                'time':[int(bar.date)],
                'open':[bar.open],
                'high':[bar.high],
                'low':[bar.low],
                'close':[bar.close],
                'volume':[bar.volume],
                'average':[bar.average]}
        newdata = pd.DataFrame(data)
        
        if self.lastbardict[reqId]:
            if bar.date == self.lastbardict[reqId].date:
                if (bar.average != self.lastbardict[reqId].average):
                    # print("IntraMinute Update: HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)
                    self.datadict[reqId].drop(self.datadict[reqId].tail(1).index,inplace=True)
                    self.datadict[reqId] = pd.concat([self.datadict[reqId],newdata],ignore_index=True)

                    if config.FrontEndDisplay and config.intraMinuteDisplay:
                        entry = self.datadict[reqId].iloc[-1]
                        sendData = {"ticker":self.tickers[reqId],"time":int(entry['time']), "open":float(entry['open']),"high":float(entry['high']),"low":float(entry['low']),"close":float(entry['close']),"volume":float(entry['volume'])}
                        
                        self.liveintraminutedata.append(sendData)

                        if len(self.liveintraminutedata) >= len(self.tickers):
                            print("Got full intraminute data package")
                        
                            try: 
                                payload = {"tickerdata":self.liveintraminutedata}
                                payload = simplejson.dumps(payload, ignore_nan=True)
                                print("Intraminute Update: " + payload)
                                self.socket.emit('update_send',payload)
                            except Exception as e: 
                                print(e)
                            
                            self.liveintraminutedata = []

            else:
                self.datadict[reqId] = pd.concat([self.datadict[reqId],newdata],ignore_index=True)
                print("got data for " +self.tickers[reqId])
                if config.FrontEndDisplay:
                    entry = self.datadict[reqId].iloc[-1]
                    sendData = {"ticker":self.tickers[reqId],"time":int(entry['time']), "open":float(entry['open']),"high":float(entry['high']),"low":float(entry['low']),"close":float(entry['close']),"volume":float(entry['volume'])}
                    self.livetickerdata.append(sendData)

                    #TODO Add catch for if not all the data comes in 
                    #(dont know if itll ever happen but would rather be safe than sorry)
                    #Maybe use a timmer from time of first minute received?
                  
                    if (len(self.livetickerdata) == len(self.tickers)): #All ticker data collected
                        print("Got All ticker data, sending data now :)")
                        for algo in self.algos:
                            # finds the right data for the algo ticker
                            algo.update(self.datadict[self.tickers.index(algo.ticker)])
                            if config.FrontEndDisplay:
                                sendData = algo.updatefrontend()
                                algodata.append(sendData)

                        try: 
                            # self.socket.emit('update_send',{)
                            payload = {"tickerdata":self.livetickerdata,"algodata":algodata}
                            payload = simplejson.dumps(payload, ignore_nan=True)
                            print(payload)
                            self.socket.emit('update_send',payload)
                        except Exception as e: 
                            print(e)

                        #reset livetickerdata so its ready to collect data for next minute
                        self.livetickerdata = []

        self.lastbardict[reqId] = bar
    




    def error(self, reqId, errorCode, errorString):
        print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)


    def backtestingDataUpdate(self):
        startpoints = {}
        numpoints = {}

        if config.FrontEndDisplay:
            algotesty.ConfigSend(self.socket)
            for i in range(len(config.tickers)):
                Fulldata = self.getDataJson(index = i)
                print("Sending Fulldata")
                try:
                    self.socket.emit('data_send', {'ticker': config.tickers[i], 'data':Fulldata})
                except Exception as e: 
                    print(e)

        for i in range(len(config.tickers)):
            startpoints[i] = self.datadict[i].shape[0]
            numpoints[i] = self.simulatedDatadict[i].shape[0] - startpoints[i]
            print("Running data loop for " + str(numpoints[i]) + " points of data for ticker " + str(config.tickers[i]) )



        starttime = datetime.now()
        for k in range(numpoints[0]):
            tickerdata = []
            algodata = []
            for i in range(len(self.tickers)):
                while(not config.updating):
                    time.sleep(1)
                entry = self.simulatedDatadict[i].iloc[startpoints[i]+k]

                self.datadict[i] = pd.concat([self.datadict[i],pd.DataFrame.from_records([entry])],ignore_index=True)
                
                if config.FrontEndDisplay:
                    sendData = {"ticker":self.tickers[i],"time":int(entry['time']), "open":float(entry['open']),"high":float(entry['high']),"low":float(entry['low']),"close":float(entry['close']),"volume":float(entry['volume'])}
                    tickerdata.append(sendData) 

            
            # algo update stuffs (assemble the algo data array)
            for algo in self.algos:
                # finds the right data for the algo ticker
                algo.update(self.datadict[self.tickers.index(algo.ticker)])
                if config.FrontEndDisplay:
                    sendData = algo.updatefrontend()
                    algodata.append(sendData)

            if config.FrontEndDisplay:
                try: 
                    # self.socket.emit('update_send',{)
                    payload = {"tickerdata":tickerdata,"algodata":algodata}
                    payload = simplejson.dumps(payload, ignore_nan=True)
                    # print(payload)
                    self.socket.emit('update_send',payload)
                except Exception as e: 
                    print(e)

            time.sleep(config.TimeDelayPerPoint)
            # print(entry[0])
   
        self.printAlgoStats()
        endtime = datetime.now()
        duration = endtime-starttime
        print("Backtesting "+str(numpoints)+" Points is Done!")
        print("Duration: " + str(duration))
        print("___________________________________________________________")
        print("--------------Press 'CTRL' to Close Program----------------")
        print("___________________________________________________________")


    def startData(self,socket, tickers, algos, warmup, duration=1):
        i = 0
        self.datadict = {}
        self.simulatedDatadict = {}
        self.livetickerdata = []
        self.liveintraminutedata = []
        self.lastbardict = {}
        self.tickers = tickers
        self.algos = algos
        self.warmup = warmup
        self.socket = socket
        for ticker in tickers:

            contract = createStockContact(ticker)

            if(config.LiveData):
                self.reqHistoricalData(i, contract, "", str(warmup) + " D", "1 min", "TRADES", 1, 2, True, [])
                

            else:
                self.datacollectednum = 0 #variable to track completed historical data pulls
                self.reqHistoricalData(i, contract, "", str(warmup+duration) + " D", "1 min", "TRADES", 1, 2, False, [])
                self.simulatedDatadict[i] = pd.DataFrame()
                self.datacollectednum = 0

            
            self.datadict[i] = pd.DataFrame()
            self.lastbardict[i] = 0
            i += 1


    def getData(self,index):
        return self.datadict[index]
    
    def get1DataPoint(self,index):
        return self.datadict[0].iloc[index]
    
    def printAlgoStats(self, FullPrint = True):
        i = 0
        for algo in self.algos:
            print("________________________________________________")
            print(f"Printing Stats for Algo {i}")
            algo.printStats(FullPrint)
            i += 1

    def getDataJson(self,index):
        result = self.datadict[index].to_json(orient="records")
        # print(result)
        return(result)
    
    def testingtrading(self):
        contract = Contract()
        contract.symbol = "TSLA"
        contract.secType = 'STK'
        contract.exchange = 'SMART'
        contract.currency = "USD"
        contract.primaryExchange = "SMART"

        order = Order()
        order.action = "Buy"
        order.totalQuantity = 1 
        order.orderType =  "MKT"
        order.eTradeOnly = False
        order.firmQuoteOnly = False

        print("done")
        print(self.reqIds(-1))
        # self.placeOrder(,contract,order)
            
