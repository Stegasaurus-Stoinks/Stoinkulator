class Trade:
    
    #unique id so find trades that have been placed by this algo

    def __init__(self, symbol, volume, ID, openPrice, openTime, direction, ibkrApi, live):
        self.symbol = symbol
        self.volume = volume
        self.ID = ID
        self.openPrice = openPrice
        self.openTime = openTime
        self.direction = direction
        self.ibkrApi = ibkrApi
        self.printInfo = True
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
            print("Closed a Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + self.ID)
            print("______________________________________________________________________")


    def closePosition(self, closePrice, closeTime):
        self.closePrice = closePrice
        self.closeTime = closeTime

        #call funtion to close order through api
        
        if self.volume > 0:
            self.ibkrApi.SimpleBuy(self.symbol, self.volume)

        if self.volume < 0:
            self.ibkrApi.SimpleSell(self.symbol, self.volume)

        self.position = False
        self.status = "Closed"

        if self.printInfo:
            print("Opened a Postion! Bought " + str(self.volume) + " of " + self.symbol + " Trade ID: " + self.ID)


    def fakeOpen(self):

        self.position = True
        self.status = "Open"

        #print to console trade placement info if asked for it
        if self.printInfo:
            print("______________________________________________________________________")
            print("Opened a fake Postion! Bought " + str(self.volume) + " of " + self.symbol + " at time: " + str(self.openTime) + " | Trade ID: " + self.ID)
            print("______________________________________________________________________")


    def fakeClose(self, closePrice, closeTime):
        self.closePrice = closePrice
        self.closeTime = closeTime

        self.position = False
        self.status = "Closed"

        if self.printInfo:
            print("Closed a fake Postion! Sold " + str(self.volume) + " of " + self.symbol + " Trade ID: " + self.ID)


    #return true or false whether we are in position or not
    def inPosition(self):
        return(self.position)

    def getStatus(self):
        return(self.status)

    def getStats(self, display = False):

        PL = self.closePrice - self.openPrice
        if self.direction == "DOWN":
            PL = PL*(-1)
        duration = self.closeTime - self.openTime
        
        if(display):
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
            print("---------Trade Stats---------")
            print("Open Price: ",self.openPrice)
            print("Close Price: ",self.closePrice)
            print("P/L: ",PL)
            print(" ")
            print("Open Time: ",self.openTime)
            print("Close Time: ",self.closeTime)
            print("Direction: ",self.direction)
            print(" ")
            print("Duration: ",duration)

        d = dict(); 
        d['openPrice'] = self.openPrice
        d['closePrice']   = self.closePrice
        d['PL']   = PL
        d['openTime']   = self.openTime
        d['closeTime']   = self.closeTime
        d['duration']   = duration

        return(d)


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