from MainStoinker.Util.IBKRHelper import *
import MainStoinker.MainStuff.Start_config as config
from MainStoinker.DataCollection.apiApi import IBapi

class Trade:
    
    #unique id so find trades that have been placed by this algo

    def __init__(self, symbol, volume, ID, openPrice, openTime, direction, stoploss, limitOrder = False, printInfo = False):
        self.ibape = IBapi()
        self.symbol = symbol
        self.volume = volume
        self.tradeID = ID
        self.stoplossId = 0
        self.openPrice = openPrice
        self.openTime = openTime
        self.direction = direction
        self.printInfo = printInfo
        self.live = config.LiveTrading
        self.limitOrder = limitOrder
        if self.direction:
            self.trailingPercent = stoploss
        else:
            self.trailingPercent = stoploss
        self.printInfo = True

        if self.live:
            self.open_position()
        else:
            self.fake_open()

        
    def fake_open(self):
        print("Open Fake Trade")

    def open_position(self):
        #call funtion to open order through api
        # TODO: Open stoploss position here too

        self.contract = makeStockContract(self.symbol)

        #check if short or long position
        if self.direction:
            if self.limitOrder:
                self.parentOrder = buyOrderObject(self.volume, limitPrice=self.openPrice)
            else:
                self.parentOrder = buyOrderObject(self.volume)
                print("buy order created")

        else:
            if self.limitOrder:
                self.parentOrder = sellOrderObject(self.volume, limitPrice=self.openPrice)
            else:
                self.parentOrder = sellOrderObject(self.volume)
        
        print(self.ibape.readPositions())
        self.ibape.getNextOrderID()
        self.parentId = self.ibape.nextValidOrderId
        print("open order id")
        print(self.parentId)
        self.ibape.placeOrder(self.parentId,self.contract,self.parentOrder)
        self.stoplossId = self.ibape.addStoploss(self.parentOrder, self.parentId, self.contract, self.trailingPercent)

        self.position = True
        self.status = "Open"

        #print to console trade placement info if asked for it
        if self.printInfo:
            print("______________________________________________________________________")
            print("Opened a Postion! Bought " + str(self.volume) + " of " + self.symbol + " Trade ID: " + str(self.tradeID))
            print("______________________________________________________________________")


    def close_position(self, closePrice, closeTime):
        self.closePrice = closePrice
        self.closeTime = closeTime

        #call funtion to close order through api
        # TODO: close stoploss position here too

        if self.live:
            if self.direction:
                if self.limitOrder:
                    self.parentCloseOrder = sellOrderObject(self.volume, limitPrice=self.openPrice)
                else:
                    self.parentCloseOrder = sellOrderObject(self.volume)

                    print("sell order created")

            else:
                if self.limitOrder:
                    self.parentCloseOrder = buyOrderObject(self.volume, limitPrice=self.openPrice)
                else:
                    self.parentCloseOrder = buyOrderObject(self.volume)
            self.ibape.cancelOrder(self.stoplossId)
            
            self.ParentCloseId = self.ibape.getNextOrderID()
            print("close order id")
            print(self.ParentCloseId)
            self.ibape.placeOrder(self.ParentCloseId,self.contract,self.parentCloseOrder)

            self.position = False
            self.status = "Closed"

            if self.printInfo:
                print("Closed a Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + str(self.tradeID))

        else:
            #Fake Trade for backtesting
            self.position = False
            self.status = "Closed"

            if self.printInfo:
                print("Closed a fake Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + str(self.tradeID))



    #return true or false whether we are in position or not
    def in_position(self):
        return(self.position)

    def get_status(self):
        return(self.status)
    
    ## old method for manual stoploss check. returns true if trade is still good
    # def check(self, curpoint):
    #     #stoploss check + reclaculation if necessary for either direction
    #     #return 1 if good 0 if bad
    #     if self.direction: #UP Trade

    #         price = curpoint["close"]
    #         if price > self.stopPrice + self.stopLoss:
    #             self.stopPrice = price - self.stopLoss

    #         if price < self.stopPrice:
    #             return 0
    #         else:
    #             return 1

    #     else: #DOWN Trade
    #         if price < self.stopPrice - self.stopLoss:
    #             self.stopPrice = price + self.stopLoss

    #         if price > self.stopLoss:
    #             return 0
    #         else:
    #             return 1

    def get_stopPrice(self,curpoint):
        self.ibape.readOrders()
        print(self.ibape.all_openorders)
        stopOrder = self.ibape.all_openorders.loc[[self.stoplossId]]
        print(stopOrder)
        price = float(curpoint["close"])-float(stopOrder["LmtPrice"])
        return price

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
    def toJson(self):
        data = {
            'symbol' : self.symbol,
            'volume' : self.volume,
            'ID' : self.ID,
            'openPrice' : self.openPrice,
            'openTime' : self.openTime,
            'direction' : self.direction
            }
        return data