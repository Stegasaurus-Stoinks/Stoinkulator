from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.common import BarData
from ibapi.common import *
from csv import writer
from datetime import datetime
from datetime import timedelta
import simplejson

from MainStoinker.Util.IBKRHelper import *

from MainStoinker.NeatTools.decorators import singleton
import MainStoinker.MainStuff.Start_config as config
from MainStoinker.Util.SocketIO_Client import FrontEndClient as Sio

import numpy as np
import pandas as pd

import threading
import time

class TestWrapper(EWrapper):
    def __init__(self):
        pass

class TestClient(EClient):
    def __init__(self, wrapper):
        EClient.__init__(self, wrapper)

@singleton
class IBapi(TestWrapper, TestClient):
    
    def __init__(self):
        # EWrapper.__init__(self)
        # EClient.__init__(self, wrapper=self)
        self.socket = Sio()
        TestWrapper.__init__(self)
        TestClient.__init__(self, wrapper=self)
        print("initializing new object")
        self.all_positions = pd.DataFrame([], columns = ['Account','Symbol', 'Quantity', 'Average Cost', 'Sec Type'])
        self.all_accounts = pd.DataFrame([], columns = ['reqId','Account', 'Tag', 'Value' , 'Currency'])
        self.all_openorders = pd.DataFrame([], columns = ['Symbol', 'OrderType', 'Quantity', 'Action', 'OrderState', 'SecType', 'AuxPrice', 'LmtPrice'])
        

    def tickPrice(self, reqId, tickType, price, attrib):
        if tickType == 2 and reqId == 1:
            print('The current ask price is: ', price)

    # collecting backtesting/warmup data
    def historicalData(self, reqId: int, bar: BarData):
        
        candleData = [datetime.fromtimestamp(int(bar.date)),int(bar.date), bar.open, bar.high, bar.low, bar.close, bar.volume]

        if(config.LiveData):
            config.tickers[reqId].append([candleData])
        else:
            self.simulatedDatadict[reqId] = self.simulatedDatadict[reqId]._append([candleData], ignore_index=True)

    # terminal callback from reqHistoricalData
    def historicalDataEnd(self, reqId: int, start: str, end: str):
        # TODO: add a warmup function for algos to analyze historical data
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
        
        if(config.LiveData):
            print(config.tickers[reqId].data)
            print("All Historical Data Collected: Live Data Starting Now...")
            # nothing else is needed here because historical data was set to keep live data
            
                
                   
        else:
            self.simulatedDatadict[reqId].columns=['date','time','open','high','low','close','volume']
            print("Historical Data Collected for " + self.tickers[reqId].name)
            self.datacollectednum += 1
            # print(self.simulatedDatadict[reqId])
            # Create datadict data frame here
            # ____________________________________________________________________________________
            firstDate = self.simulatedDatadict[reqId].at[0,'date']
            startDate = firstDate + timedelta(days=self.warmup)
            print("Warmup Start Date: " + str(firstDate))
            print("Warmup End Date: " + str(startDate))

            self.tickers[reqId].data = self.simulatedDatadict[reqId].loc[(self.simulatedDatadict[reqId]['date'] < startDate)]
            self.tickers[reqId].data.columns=['date','time', 'open','high','low','close','volume']

            if self.datacollectednum >= len(self.tickers): #all historical data collected
                print("------All Historical Data Collected------")
                self.eventDict[0].set() 
        
        if config.FrontEndDisplay:
            self.socket.send_full_data(reqId)




    def historicalDataUpdate(self, reqId: int, bar: BarData):           # Live Data Updates
        ticker = config.tickers[reqId]
        # TODO: maybe make this a method (2)
        candleData = [datetime.fromtimestamp(int(bar.date)),int(bar.date), bar.open, bar.high, bar.low, bar.close, bar.volume]

            # is it intraminute?
        self.lastbar = ticker.data.iloc[-1]
        lastbartime = self.lastbar["date"].to_pydatetime()
        if candleData[0] == lastbartime:
            # did anything change?
            if (bar.volume != self.lastbar["volume"]):
                print("intraminute update")
                ticker.replace([candleData])
                if config.intraMinuteDisplay:
                    ticker.intraminute_update()
        #it is not intraminute
        else:
            ticker.append([candleData])
            print(ticker.data)
            self.eventDict[reqId].set()              


    def error(self, reqId, errorCode, errorString):
        print("Error. Id: ", reqId, " Code: ", errorCode, " Msg: ", errorString)



    def startData(self, tickers, algos, warmup, eventDict, duration=1):
        self.eventDict = eventDict
        self.datadict = {}
        self.simulatedDatadict = {}
        self.livetickerdata = []
        self.liveintraminutedata = []
        self.lastbardict = {}
        self.tickers = tickers
        self.algos = algos
        self.warmup = warmup
        for ticker in tickers.values():
            
            if ticker.name == "ETH" or ticker.name == "BTC":
                contract = create_crypto_contract(ticker.name)

            else:
                contract = create_stock_contract(ticker.name)

            if(config.LiveData):
                self.reqHistoricalData(ticker.index, contract, "", str(warmup) + " D", "1 min", "TRADES", 1, 2, True, [])
                

            else:
                self.datacollectednum = 0 #variable to track completed historical data pulls
                self.reqHistoricalData(ticker.index, contract, "", str(warmup+duration) + " D", "1 min", "TRADES", 1, 2, False, [])
                self.simulatedDatadict[ticker.index] = pd.DataFrame()
                self.datacollectednum = 0

            
            self.datadict[ticker.index] = pd.DataFrame()
            self.lastbardict[ticker.index] = 0

        print("startData read positions")
        print(self.readPositions())

        print(self.readOrders())


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
        # super().openOrder(orderId, contract, order, orderState)
        # print("OpenOrder. PermId:", (order.permId), "ClientId:", (order.clientId), "OrderId:", (orderId), 
        #     "Account:", order.account, "Symbol:", contract.symbol, "SecType:", contract.secType,
        #     "Exchange:", contract.exchange, "Action:", order.action, "OrderType:", order.orderType,
        #     "TotalQty:", (order.totalQuantity), "CashQty:", (order.cashQty), 
        #     "LmtPrice:", (order.lmtPrice), "AuxPrice:", (order.auxPrice), "Status:", orderState.status,
        #     "MinCompeteSize:", (order.minCompeteSize))
        self.all_openorders.loc[orderId]= {'Symbol':contract.symbol, 'OrderType':order.orderType, 'Quantity':order.totalQuantity, 'Action':order.action, 'OrderState':orderState.status,'SecType':contract.secType, 'AuxPrice:': float(order.auxPrice),'LmtPrice': float(order.lmtPrice)}

    def openOrderEnd(self):
        if config.Debug:
            print("openOrderEnd CallBack")
        try:
            self.orders_event_obj.set()
        except Exception as e:
            if config.Debug:
                print(e)
                print("failed to set event object for readOrders")
    


    
    def addStoploss(self, parentOrder, parentOrderID, contract, trailingPercent):
        #StopId being set means you are updating a stoploss thats already been created

        parentAction = parentOrder.action
        quantity = parentOrder.totalQuantity
        parentOrderId = parentOrder.orderId
        
        self.getNextOrderID()
        OrderId = self.nextValidOrderId
        print(OrderId)

        stopLoss = Order()
        stopLoss.orderId = OrderId
        if parentAction == "Buy":
            stopLoss.action = "SELL"  
        else: 
            stopLoss.action = "BUY"

        stopLoss.orderType = "TRAIL"
        
        #Stop trigger price
        stopLoss.trailStopPrice = trailingPercent
        stopLoss.auxPrice = trailingPercent
        stopLoss.totalQuantity = quantity
        stopLoss.parentId = parentOrderId
        stopLoss.eTradeOnly = False
        stopLoss.firmQuoteOnly = False

        self.placeOrder(OrderId, contract, stopLoss)

        return OrderId
    

    def error(self, reqId:TickerId, errorCode:int, errorString:str, advancedOrderRejectJson = ""):
        if reqId > -1:
            print("Error. Id: " , reqId, " Code: " , errorCode , " Msg: " , errorString)
        
