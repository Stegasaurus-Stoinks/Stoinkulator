from MainStoinker.Util.IBKRHelper import *
#needs to be changed back to #import MainStoinker.MainStuff.Start_config as config
import MainStoinker.MainStuff.Start_config as config
from MainStoinker.DataCollection.apiApi import IBapi
import pandas as pd
import MainStoinker.MainStuff.main_utils as utils
from MainStoinker.DataCollection.ExecutionsLogger import ExecutionLog
import random

class Trade:
    
    #unique id so find trades that have been placed by this algo

    #OpenPrice is the price the algo tried to buy at, which will likely be different from the entryPrice, same for openTime vs entryTime
    #used for tracking entry discrpencies since we are going to start with market orders and not limit orders

    def __init__(self, symbol, volume, ID, openPrice, openTime, direction, logger, limitOrder = False):
        self.ibape = IBapi()
        self.symbol = symbol
        self.volume = volume
        self.tradeID = ID
        self.stoplossId = 0
        self.stopLossType = "Trailing" #Trailing or Fixed
        self.openPrice = openPrice
        self.openTime = openTime
        self.closePrice = 0
        self.closeTime = 0
        self.direction = direction
        self.limitOrder = limitOrder
        self.tpOrder = 0
        self.tp = 0
        
        # TODO: need to get this data from executions of parent order
        #these represent the actual trade price and time from ibapi
        self.ibEntryPrice = 0
        self.ibEntryTime = 0
        #same for close, will be updated in some function
        self.ibClosePrice = 0
        self.ibCloseTime = 0
        
        self.ocaGroupName = "oca"+str(ID)+str(random.randint(0,100))

        self.logger = logger
        
        self.executionLog = ExecutionLog()

        # setting hardcoded emergency stoploss. Can be changed with functions
        # set trailingPercent to be the exact amount above or below 1 for equations
        if self.direction:
            trailingPercent = 1 - 0.01
        else:
            trailingPercent = 1 + 0.01
        self.stopPrice = round(openPrice * (trailingPercent), 2)
        self.stopDelta = abs(openPrice - self.stopPrice)
        

        if config.LiveTrading:
            self.open_position()
        else:
            self.fake_open()

        
    def fake_open(self):
        print("Open Fake Trade")

    def open_position(self):
        #call funtion to open order through api
        if self.symbol == "ETH" or self.symbol == "BTC":
            self.contract = create_crypto_contract(self.symbol)
        else:
            self.contract = create_stock_contract(self.symbol)


        #check if short or long position
        if self.direction:
            if self.limitOrder:
                self.parentOrder = buy_order_object(self.volume, limitPrice=self.openPrice)
            else:
                self.parentOrder = buy_order_object(self.volume)
                print("buy order created")

        else:
            if self.limitOrder:
                self.parentOrder = sell_order_object(self.volume, limitPrice=self.openPrice)
            else:
                self.parentOrder = sell_order_object(self.volume)
        

        self.ibape.getNextOrderID()
        self.parentId = self.ibape.nextValidOrderId

        # "20200923 15:13:20 EST"
        #TODO Fix time error, IBKR not happy with Timezone format
        temptime = self.openTime + pd.Timedelta(2,"min")
        temptime = temptime.strftime('%X')
        self.parentOrder.tif = "GTD"
        self.parentOrder.goodTillDate = temptime
        self.logger.debug("Order Valid Until: "+ str(temptime))
        self.logger.debug("Open Order ID: "+ str(self.parentId))
        self.parentOrder.orderId = self.parentId
        
        self.ibape.placeOrder(self.parentId,self.contract,self.parentOrder)

        #set stoploss
        #TODO Move into addstoploss pass group name
        try:
            self.stopOrder = self.ibape.addStoploss(self.parentOrder, self.stopPrice)
            self.stopOrder.ocaGroup = self.ocaGroupName
            self.stopOrder.ocaType = 2 #proportial reduction
            self.stopOrder.transmit = True
            self.ibape.placeOrder(self.stopOrder.orderId, self.contract, self.stopOrder)
        except:
            self.stopOrder = self.ibape.addStoploss(self.parentOrder, self.stopPrice)
            self.ibape.placeOrder(self.stopOrder.orderId, self.contract, self.stopOrder)

        self.logger.debug("Stoploss OrderId:" + str(self.stopOrder.orderId))


        self.position = True
        self.status = "Open"

        #print to console trade placement info if asked for it
        self.logger.info(str(self.tradeID) + " - Opened a Postion! Bought " + str(self.volume) + " of " + str(self.symbol) + " Trade ID: " + str(self.parentId))


    def close_position(self, closePrice, closeTime):
        self.closePrice = closePrice
        self.closeTime = closeTime

        #call funtion to close order through api

        if config.LiveTrading:
            if self.direction:
                if self.limitOrder:
                    self.parentCloseOrder = sell_order_object(self.volume, limitPrice=self.openPrice)
                else:
                    self.parentCloseOrder = sell_order_object(self.volume)

                    self.logger.debug("sell order created in trade object")

            else:
                if self.limitOrder:
                    self.parentCloseOrder = buy_order_object(self.volume, limitPrice=self.openPrice)
                else:
                    self.parentCloseOrder = buy_order_object(self.volume)

            self.parentCloseOrder.ocaGroup = self.ocaGroupName
            self.parentCloseOrder.ocaType = 2

            #oca group handles the closing of the stoploss
            # self.logger.debug("StopLoss Order Id: "+str(self.stoplossId))
            # self.ibape.cancelOrder(self.stoplossId)
            
            self.ParentCloseId = self.ibape.getNextOrderID()
            self.parentCloseOrder.orderId = self.ParentCloseId
            self.logger.debug("Parent Close Order ID " + str(self.ParentCloseId))
            self.ibape.placeOrder(self.ParentCloseId,self.contract,self.parentCloseOrder)

            self.position = False
            self.status = "Closed"

            self.logger.info(str(self.tradeID)+" - Closed a Position! Sold " + str(self.volume) + " of " + str(self.symbol) + " Trade ID: " + str(self.tradeID) +"\n")

        else:
            #Fake Trade for backtesting
            self.position = False
            self.status = "Closed"

            print("Closed a fake Postion! Sold " + str(self.volume) + " of " + str(self.symbol) + " Trade ID: " + str(self.tradeID))



    #return true or false whether we are in position or not
    def in_position(self):
        return(self.position)

    def get_status(self):
        return(self.status)
    
    
    # manual stoploss check. returns true if trade is still good
    def check_stoploss(self, curpoint, value='close'):
        result = 1
        price = curpoint[value]
        if config.LiveTrading:
            self.logger.debug(str(self.tradeID)+" - printing open orders, looking for "+str(self.stopOrder.orderId))
            self.logger.debug(str(self.tradeID)+" - "+str(self.ibape.all_openorders))
            if self.stopOrder.orderId in self.ibape.all_openorders.index:
                if self.ibape.all_openorders.loc[self.stopOrder.orderId,'OrderState'] == 'Filled':
                    self.executionLog.return_execution_details(self.stopOrder.orderId)
                    self.logger.info("Got Execution Details for STOPLOSS that closed the trade :D")
                    return 0
                else:
                    self.logger.debug("OrderState for stoploss is not filled... its " + str(self.ibape.all_openorders.loc[self.stopOrder.orderId,'OrderState']))
                    
            else:
                self.logger.info(str(self.tradeID)+" - StopLoss not found in Open Orders... ")
                # We should only hit this if the order gets canceled, which means either the TP hit or we manually closed the position.
                
                
                # stopOrderExecutionDetails = self.executionLog.return_execution_details(self.stopOrder.orderId)
                # # for reference, Execution details look like this:
                # # [OrderId, PermId, ib.Execution, ib.Contract, ib.CompletedOrder]
                # self.closePrice = stopOrderExecutionDetails[2].price
                return 0
            
            
        #stoploss check + reclaculation if necessary for either direction
        #return 1 if good 0 if bad
        if self.direction: #UP Trade

            if (self.stopLossType == 'Trailing') and (price > (self.stopPrice + self.stopDelta)):
                self.set_stopPrice(price - self.stopDelta)
                result = 1

            if price < self.stopPrice:
                self.logger.info(str(self.tradeID)+" - Manually closing position based on stoploss: "+str(self.tradeID))
                self.close_position(self.stopPrice,curpoint['date'])
                result = 0    

        else: #DOWN Trade
            if (self.stopLossType == 'Trailing') and (price < (self.stopPrice - self.stopDelta)):
                self.set_stopPrice(price + self.stopDelta)
                result = 1

            if price > self.stopPrice:
                self.logger.info(str(self.tradeID)+" - Manually closing position based on stoploss: "+str(self.tradeID))        
                self.close_position(self.stopPrice,curpoint['date']) 
                result = 0
        
        return result
    

    def create_tp(self, tp, quantity):
        self.tp = tp

        if config.LiveTrading:
            if self.tpOrder:
                print("Modifying TP order")
                self.tpOrder.lmtPrice = self.tp
                self.tpOrder.totalQuantity = quantity
                self.ibape.placeOrder(self.tpOrderId, self.contract, self.tpOrder)
            else:        
                print("Creating New TP order")
                self.tpOrder = self.ibape.addTP(self.parentOrder, self.tp, quantity)

                self.tpOrder.ocaGroup = self.ocaGroupName
                self.tpOrder.ocaType = 2 #Remaining orders are proportionately reduced in size with block

                self.tpOrderId = self.tpOrder.orderId
                self.ibape.placeOrder(self.tpOrderId, self.contract, self.tpOrder)


        print("TP set to ", self.tp)


    #check to see if we should take profit
    def check_tp(self, curpoint):
        result = 0
        
        if config.LiveTrading:
            self.logger.debug(str(self.tradeID)+" - printing open orders, looking for "+str(self.tpOrder.orderId))
            self.logger.debug(str(self.tradeID)+" - "+str(self.ibape.all_openorders))
            if self.tpOrder.orderId in self.ibape.all_openorders.index:
                if self.ibape.all_openorders.loc[self.tpOrder.orderId,'OrderState'] == 'Filled':
                    self.executionLog.return_execution_details(self.tpOrder.orderId)
                    self.logger.info("Got Execution Details for TAKE PROFIT that closed the trade :D")
                    return 0
            else:
                self.logger.info(str(self.tradeID)+" - TP not in Open Orders... ")
                # We should only hit this if the order gets canceled, which means either the StopLoss hit or we manually closed the position.
 
            
        if self.direction:
            if curpoint['high'] > self.tp:
                self.close_position(self.tp, curpoint['date'])
                self.logger.info(str(self.tradeID)+" - Manually closing position based on TP: "+str(self.tradeID))
                result = 1

        else:
            if curpoint['low'] < self.tp:
                self.close_position(self.tp, curpoint['date'])
                self.logger.info(str(self.tradeID)+" - Manually closing position based on TP: "+str(self.tradeID))
                result = 1

        return result
    

    def update_tp(self, tp):
        self.create_tp(tp,self.tpOrder.totalQuantity)



    def set_stopLossType(self, typeofstoploss):
        self.stopLossType = typeofstoploss

    # tying the stopOrder update to set_stopPrice because realistically we never want these to be out of sync to avoid misinformation
    def set_stopPrice(self, price):
        self.stopPrice = price
        if config.LiveTrading:
            self.stopOrder.auxPrice = price
            self.logger.debug(str(self.tradeID)+" - updating auxPrice for "+str(self.symbol)+": " + str(self.stopOrder.auxPrice))
            self.ibape.placeOrder(self.stopOrder.orderId,self.contract,self.stopOrder)

    def set_stopDelta(self, value):
        self.stopDelta = value




    def get_stats(self, Fulldisplay = True):

        PL = self.closePrice - self.openPrice
        if self.direction == "DOWN":
            PL = PL*(-1)
        duration = self.closeTime - self.openTime
        
        f = open("tradey.txt", "a")
        f.write("---------Trade Stats---------\n")
        f.write(str("Open Price: "+str(self.openPrice)+"\n"))
        f.write(str("Close Price: "+str(self.closePrice)+"\n"))
        f.write(str("P/L: "+str(PL)+"\n\n"))
        f.write(str("Open Time: "+str(self.openTime)+"\n"))
        f.write(str("Close Time: "+str(self.closeTime)+"\n"))
        f.write(str("Direction: "+str(self.direction)+"\n\n"))
        f.write(str("Duration: "+str(duration)+"\n\n\n\n"))
        f.close()
        
        if(Fulldisplay):
            print("---------Trade Stats---------")
            print("ID: ",self.tradeID)
            print("Open Price: ",self.openPrice)
            print("Close Price: ",self.closePrice)
            print("P/L: ",PL)
            print(" ")
            print("Open Time: ",self.openTime)
            print("Close Time: ",self.closeTime)
            print("Duration: ",duration)
            print(" ")
            print("Direction: ",self.direction)
            print("-----------------------------")

        
        else:
            print("Trade ", self.ID)
            print("Profit: ",PL)

        d = dict(); 
        d['openPrice'] = self.openPrice
        d['closePrice']   = self.closePrice
        d['PL']   = PL
        d['openTime']   = self.openTime
        d['closeTime']   = self.closeTime
        d['duration']   = duration

        return(d)


    def get_profit(self):
        PL = self.closePrice - self.openPrice
        if self.direction == "DOWN":
            PL = PL*(-1)
        return PL


    #returns a dictionary object of all data needed to recreate the trade object
    def to_json(self):
        if self.status == 'Closed':
            duration = self.closeTime - self.openTime
            profit = self.closePrice - self.openPrice

        else:
            self.closePrice = float('nan')
            self.closeTime = float('nan')
            duration = float('nan')
            profit = float('nan')

        data = {
            'symbol' : self.symbol,
            'ID' : self.tradeID,
            'stoplossID' : self.stopOrder.orderId,
            'volume' : self.volume,
            'openPrice' : self.openPrice,
            'openTime' : self.openTime,
            'direction' : self.direction,
            'stopDelta' : self.stopDelta,
            'status' : self.status,
            'closePrice' : self.closePrice,
            'closeTime' : self.closeTime,
            'duration' : duration,
            'profit' : profit
            }
        return data
    