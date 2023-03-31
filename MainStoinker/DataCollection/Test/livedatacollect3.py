from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.common import BarData
from csv import writer
import datetime

import numpy as np
import pandas as pd

import threading
import time


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

        candleData = [bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
        self.datadict[reqId] = self.datadict[reqId].append([candleData], ignore_index=True)

    # print(candleData)
    # print(self.test)
    # np.append(self.dataArray,[candleData])

    def historicalDataEnd(self, reqId: int, start: str, end: str):
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)

        # print(self.df)
        f = open("./MainStoinker/DataCollection/Test/liveData_"+self.tickers[reqId]+".csv", "w")
        self.datadict[reqId].to_csv(f, index=False, header=False, lineterminator='\n')
        f.close()
        print("All Historical Data Collected: Live Data Starting Now...")
        # self.disconnect()

    # print("Finished")

    def historicalDataUpdate(self, reqId: int, bar: BarData):
        if self.lastbardict[reqId] != 0:
            if (bar.average != self.lastbardict[reqId].average):
                print("HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)

                # Open our existing CSV file in append mode
                # Create a file object for this file
                candleData = [bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]
                tickerdata = self.datadict[reqId]
                lastindex = tickerdata.tail(1).index

                if(self.lastbardict[reqId].date == bar.date):
                    tickerdata.drop(lastindex,inplace=True)

                tickerdata.loc[lastindex] = candleData

                self.lastbardict[reqId] = bar

        else:
            self.lastbardict[reqId] = bar

    def error(self, reqId, errorCode, errorString):
        print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)

    def startData(self, tickers):
        i = 0
        self.datadict = {}
        self.lastbardict = {}
        self.tickers = tickers
        for ticker in tickers:
            print("testy")
            queryTime = (datetime.datetime.today() - datetime.timedelta(days=0)).strftime("%Y%m%d %H:%M:%S")

            contract = Contract()
            contract.secType = "STK"
            contract.symbol = ticker
            contract.currency = "USD"
            contract.exchange = "SMART"

            self.reqHistoricalData(i, contract, "", "1 D", "1 min", "TRADES", 1, 1, True, [])

            f = open("./MainStoinker/DataCollection/Test/liveData_"+ticker+".csv", "w")
            f.truncate()
            f.close()
            
            self.datadict[i] = pd.DataFrame()
            i += 1
