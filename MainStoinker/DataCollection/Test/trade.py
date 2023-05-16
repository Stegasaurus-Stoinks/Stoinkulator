class Trade:
    
    #unique id so find trades that have been placed by this algo

    def __init__(self, symbol, volume, ID, openPrice, openTime, direction, live, stoploss, printInfo = False):
        self.symbol = symbol
        self.volume = volume
        self.ID = ID
        self.openPrice = openPrice
        self.openTime = openTime
        self.direction = direction
        self.printInfo = printInfo
        self.live = live
        if self.direction:
            self.stopPrice = openPrice - stoploss
        else:
            self.stopPrice = openPrice + stoploss

        self.stopLoss = stoploss
        if live:
            self.openPosition()
        else:
            self.fakeOpen()
        


    def openPosition(self):
        #call funtion to open order through api

        #check if short or long position
        if self.volume > 0:
            self.ibkrApi.SimpleBuy(self.symbol, self.volume)

        if self.volume < 0:
            self.ibkrApi.SimpleSell(self.symbol, self.volume)

        self.position = True
        self.status = "Open"

        #print to console trade placement info if asked for it
        if self.printInfo:
            print("______________________________________________________________________")
            print("Closed a Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + str(self.ID))
            print("______________________________________________________________________")


    def closePosition(self, closePrice, closeTime):
        self.closePrice = closePrice
        self.closeTime = closeTime

        #call funtion to close order through api
        
        if self.live:
            if self.volume > 0:
                self.ibkrApi.SimpleBuy(self.symbol, self.volume)

            if self.volume < 0:
                self.ibkrApi.SimpleSell(self.symbol, self.volume)

            self.position = False
            self.status = "Closed"

            if self.printInfo:
                print("Closed a Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + str(self.ID))

        else:
            #Fake Trade for backtesting
            self.position = False
            self.status = "Closed"

            if self.printInfo:
                print("Closed a fake Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + str(self.ID))


    def fakeOpen(self):

        self.position = True
        self.status = "Open"

        #print to console trade placement info if asked for it
        if self.printInfo:
            print("______________________________________________________________________")
            print("Opened a fake Postion! Bought " + str(self.volume) + " of " + self.symbol + " at time: " + str(self.openTime) + " | Trade ID: " + str(self.ID))
            print("______________________________________________________________________")



    #return true or false whether we are in position or not
    def inPosition(self):
        return(self.position)

    def getStatus(self):
        return(self.status)
    
    def check(self, curpoint):
        #stoploss check + reclaculation if necessary for either direction
        #return 1 if good 0 if bad
        if self.direction: #UP Trade
            # print(curpoint['Close'])
            # print(self.stopPrice)
            price = curpoint["Close"]
            if price > self.stopPrice + self.stopLoss:
                self.stopPrice = price - self.stopLoss
                return 1
            if price < self.stopPrice:
                return 0
            else:
                return 1

        # else: #DOWN Trade
        #     if price < self.stopPrice - self.stopLoss:
        #         self.stopPrice = price + self.stopLoss
        #         return 1
        #     if price > self.stopLoss:
        #         return 0

    def getStats(self, Fulldisplay = True):

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
            print("ID: ",self.ID)
            print("Open Price: ",self.openPrice)
            print("Close Price: ",self.closePrice)
            print("P/L: ",PL)
            print(" ")
            print("Open Time: ",self.openTime)
            print("Close Time: ",self.closeTime)
            print("Direction: ",self.direction)
            print(" ")
            print("Duration: ",duration)

        
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


    def getProfit(self):
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