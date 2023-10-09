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

from IBKRHelper import *

import queue
# from NeatTools.decorators import singleton
import Start_config as config

import numpy as np
import pandas as pd

import threading
import time

import json

from testyclass import testibkr
from trade import Trade
from EMACrossing_Algo import Algo


def createStockContact(ticker: str):
    contract = Contract()
    contract.secType = "STK"
    contract.symbol = ticker
    contract.currency = "USD"
    contract.exchange = "SMART"

    return contract

# @singleton
class TestWrapper(EWrapper):
    def __init__(self):
        pass

class TestClient(EClient):
    def __init__(self, wrapper):
        EClient.__init__(self, wrapper)

class IBapi(TestWrapper, TestClient):
    # _instance = None
    # _lock = threading.Lock()

    # def __new__(cls):
    #     if cls._instance is None: 
    #         with cls._lock:
    #             # Another thread could have created the instance
    #             # before we acquired the lock. So check that the
    #             # instance is still nonexistent.
    #             if not cls._instance:
    #                 print("making new instance")
    #                 cls._instance = super().__new__(cls)
    #     return cls._instance
    
    def __init__(self):
        # EWrapper.__init__(self)
        # EClient.__init__(self, wrapper=self)
        TestWrapper.__init__(self)
        TestClient.__init__(self, wrapper=self)
        print("initializing new object")
        self.all_positions = pd.DataFrame([], columns = ['Account','Symbol', 'Quantity', 'Average Cost', 'Sec Type'])
        self.all_accounts = pd.DataFrame([], columns = ['reqId','Account', 'Tag', 'Value' , 'Currency'])
        self.all_openorders = pd.DataFrame([], columns = ['Symbol', 'Order Type', 'Quantity', 'Action', 'Order State', 'Sec Type'])
        

    def tickPrice(self, reqId, tickType, price, attrib):
        if tickType == 2 and reqId == 1:
            print('The current ask price is: ', price)

    def historicalData(self, reqId: int, bar: BarData):
        # print("HistoricalData. ReqId:", reqId, "BarData.", bar)
        candleData = [datetime.fromtimestamp(int(bar.date)),int(bar.date), bar.open, bar.high, bar.low, bar.close, bar.volume, bar.average]

        if(config.LiveData):
            self.datadict[reqId] = self.datadict[reqId]._append([candleData], ignore_index=True)
        else:
            self.simulatedDatadict[reqId] = self.simulatedDatadict[reqId]._append([candleData], ignore_index=True)


    def historicalDataEnd(self, reqId: int, start: str, end: str):
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        
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

                print("historical data end read positions:")
                print(self.readPositions())

                # threadtest = threading.Thread(target=self.backtestingDataUpdate(),daemon=True)
                # threadtest.start()
                # self.backtestingDataUpdate()




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
                            tempdata = self.datadict[self.tickers.index(algo.ticker)]
                            algo.update(self, tempdata)
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
            tickerfulldata = []
            algotesty.ConfigSend(self.socket)
            for i in range(len(config.tickers)):
                Fulldata = self.getDataJson(index = i)
                tickerfulldata.append({'ticker': config.tickers[i], 'data':Fulldata})
                # try:
                #     self.socket.emit('data_send', {'ticker': config.tickers[i], 'data':Fulldata})
                # except Exception as e: 
                #     print(e)
            print("Sending Fulldata")
            try: 
                # self.socket.emit('update_send',{)
                payload = {"tickerdata":tickerfulldata}
                payload = simplejson.dumps(payload, ignore_nan=True)
                # print(payload)
                self.socket.emit('data_send',payload)
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
                print(self.readPositions())
                # self.testingtrading(self.algos)
                algo.testpositions(self, 42069)
                algo.update(self, self.datadict[self.tickers.index(algo.ticker)])

                # print(self)

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
            print(entry[0])
   
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

        print("startData read positions")
        print(self.readPositions())


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
    
    def testingtrading(self,algos):
        # contract = makeStockContract("MSFT")
        time.sleep(2)

        # parentorder = buyOrderObject(1)
        # parentId = self.nextValidOrderId
        # self.placeOrder(parentId,contract,parentorder)
        # self.readOrders()
        # print("order1 placed")
        # print("orderid = " + str(parentId))
        # stoplossId = self.addStoploss(parentorder, parentId, contract, 100)

        # # self.getNextOrderID()
        # # self.placeOrder(self.nextValidOrderId,contract,buyOrderObject(2))
        # # print("order2 placed")
        # # print("orderid = " + str(self.nextValidOrderId))
        
        # time.sleep(1)

        print("Read positions in livedatacollect:")
        print(self.readPositions())
        time.sleep(2)
        temp = testibkr(self)
        time.sleep(2)
        temp.testpositions(self)
        time.sleep(2)
        
        trade = Trade("AAPL", 10, 5, 3.00, 5555, 5, config.LiveTrading, 0.20, self, printInfo=False)

        print("here")
        for algo in algos:
            algo.testpositions(self, 42069)

        # print(self.readPositions("TSLA"))

        # time.sleep(10)
        # print("changing stoploss")
        # self.addStoploss(parentorder, parentId, contract, stopPrice=150, StopId = stoplossId)

        # time.sleep(5)
        # # Changing StopLoss to Market Order To Close both orders
        # self.addStoploss(parentorder, parentId, contract, stopPrice=150, StopId = stoplossId, OrderType="MKT")

        # self.readOrders()

        # testsingleton()
    
        # time.sleep(5)
        # self.readOrders()
        # time.sleep(20)
        # self.readOrders()

        # self.getNextOrderID()
        # self.placeOrder(self.nextValidOrderId,contract,sellorder)
        # print("order3 placed")
        # print("orderid = " + str(self.nextValidOrderId))
        # print(self.readPositions())

    def getNextOrderID(self):
        self.event_obj = threading.Event()
        self.reqIds(-1)
        if config.Debug:
            print("waiting for getNextOrderID thread")
        timeout = 2
        flag = self.event_obj.wait(timeout)
        if flag:
            if config.Debug:
                print("getNextOrderID Event Triggered, This means it worked")
            return self.nextValidOrderId
        else:
            if config.Debug:
                print("Time out occured, getNextOrderID event internal flag still false. Executing thread without waiting for event")
            return None

    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)

        self.nextValidOrderId = orderId
        if config.Debug:
            print("NextValidId:", orderId)
        try:
            self.event_obj.set()
        except Exception as e:
            if config.Debug:
                print(e)
                print("tried to set event object for getNextOrderID")

    #Generate new list of positions, returns Pandas DataFrame
    def readPositions(self,tickerSymbol:str = None):
        self.positions_event_obj = threading.Event()
        self.temp = self.reqPositions() # associated callback: position
        # self.reqPositionsMulti()
        if config.Debug:
            print("Waiting for IB's API response for accounts positions requests...")
        # time.sleep(3)
        timeout = 2
        flag = self.positions_event_obj.wait(timeout)
        if flag:
            current_positions = self.all_positions # associated callback: position
            # dont know why i cant shift the index of the array, adding line below breaks stuff :(
            # current_positions.set_index('Account',inplace=True,drop=True) #set all_positions DataFrame index to "Account"
            if tickerSymbol:
                return current_positions.loc[current_positions['Symbol'] == tickerSymbol]
            return current_positions
        else:
            print("error with callback for positions")
    
    def position(self, account, contract, pos, avgCost):
        index = str(account)+str(contract.symbol)
        if config.Debug:
            print("In CallBack for Postions: Position Data Received")
            print(account, contract.symbol, pos, avgCost, contract.secType)
        self.all_positions.loc[index]= {'Account':account, 'Symbol':contract.symbol, 'Quantity':pos, 'Average Cost':avgCost, 'Sec Type':contract.secType}
            
    def positionEnd(self, event_obj = None):
        # super().positionEnd()
        if config.Debug:
            print("PositionEnd CallBack")
        try:
            self.positions_event_obj.set()
        except Exception as e:
            if config.Debug:
                print(e)
                print("failed to set event object for readPositions")

    def readOrders(self):
        self.orders_event_obj = threading.Event()
        self.reqAllOpenOrders()
        if config.Debug:
            print("Waiting for IB's API response for accounts positions requests...")
        # time.sleep(3)
        timeout = 2
        flag = self.orders_event_obj.wait(timeout)
        if flag:
            print(self.all_openorders)
        else:
            print("error with callback for positions")

    def openOrder(self,orderId,contract,order,orderState):
        self.all_openorders.loc[orderId]= {'Symbol':contract.symbol, 'Order Type':order.orderType, 'Quantity':order.totalQuantity, 'Action':order.action, 'Order State':orderState.status,'Sec Type':contract.secType}

    def openOrderEnd(self):
        if config.Debug:
            print("openOrderEnd CallBack")
        try:
            self.orders_event_obj.set()
        except Exception as e:
            if config.Debug:
                print(e)
                print("failed to set event object for readOrders")

    
    def addStoploss(self, parentOrder, parentOrderID, contract, stopPrice, StopId = None, OrderType = None):
        #StopId being set means you are updating a stoploss thats already been created

        parentAction = parentOrder.action
        quantity = parentOrder.totalQuantity
        parentOrderId = parentOrder.orderId
        if StopId == None:
            self.getNextOrderID()
            OrderId = self.nextValidOrderId
        else:
            OrderId = StopId
            if config.Debug:
                print("Editing Stoploss Price/Quantity")

        stopLoss = Order()
        stopLoss.orderId = OrderId
        if parentAction == "Buy":
            stopLoss.action = "SELL"  
        else: 
            stopLoss.action = "BUY"

        if OrderType == None:
            stopLoss.orderType = "STP"
        else: 
            stopLoss.orderType = OrderType
            if config.Debug:
                print("Editing Stoploss Order Type to " + str(OrderType))
        #Stop trigger price
        stopLoss.auxPrice = stopPrice
        stopLoss.totalQuantity = quantity
        stopLoss.parentId = parentOrderId
        stopLoss.eTradeOnly = False
        stopLoss.firmQuoteOnly = False

        self.placeOrder(OrderId, contract, stopLoss)

        return OrderId
    
    # def readPositionsTest(self,tickerSymbol:str = None):
    #     self.readPositions(self)

    def error(self, reqId:TickerId, errorCode:int, errorString:str, advancedOrderRejectJson = ""):
        if reqId > -1:
            print("Error. Id: " , reqId, " Code: " , errorCode , " Msg: " , errorString)
        


def testsingleton():
    app = IBapi()
    # app.connect('127.0.0.1', 7497, 123)
    while(not app.isConnected):
        time.sleep(.5)
    print("TWS Connected")
    print(app.readPositions("TSLA"))

# TODO: Singleton not working yet... dont know why, its not connecting...