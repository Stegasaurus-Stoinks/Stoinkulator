# FVG CLASS

class FVG:
    # States:
    # VALID
    # TESTED (first test: must close outside of FVG, low must be in FVG)
    # RETESTED  (second test: must close in or low must be in FVG)
    # FULLYTESTED  (low of candle fully swept range of FVG)
    # TRADED    (Trade was placed on this gap, not going to trade it again)
    # INVALID   (FVG was invalidated by closing outside of the range on the wrong side)

    def __init__(self, upperbound, lowerbound, time, direction):
        self.upperbound = upperbound
        self.lowerbound = lowerbound
        self.startTime = time
        self.direction = direction
        self.Status = "VALID"
        self.range = abs(self.upperbound-self.lowerbound)

    def updateFVG(self, currentData):
        if self.Status == "TRADED":
            return self.Status
        
        #if up candles formed the FVG
        if self.direction:
            if currentData['close'] < self.lowerbound:
                self.Status = "INVALID"
                return self.Status
                #TODO add timeout section to the update
            
            if self.Status == "VALID":
                if currentData['close'] > self.upperbound and currentData['low'] < self.upperbound:
                    self.Status = "TESTED"
                    if currentData['low'] < self.lowerbound:
                        self.Status = "FULLYTESTED"

                #riskier trade entries

                #if close is inside of top half of the range
                wicksize = min(currentData['close'],currentData['open']) - currentData['low']
                bodysize = abs(currentData['close'] - currentData['open'])
                if currentData['close'] < self.upperbound and currentData['close'] > self.lowerbound + self.range/2:
                    # if wick is comparatively big, but not too big
                    if wicksize > self.range*0.5 and wicksize < self.range*2 and wicksize > bodysize:
                        self.Status = "FULLYTESTED"
                    return self.Status
                
                #if wick is big and ends in lower half of range
                if wicksize > self.range*0.5 and wicksize < self.range*2 and wicksize > bodysize:
                    if (self.lowerbound-(self.range/2)) < currentData['low'] < (self.lowerbound+(self.range/2)):
                        self.Status = "FULLYTESTED"
                        return self.Status
                
                
            if self.Status == "TESTED":
                if self.checkifinrange(currentData['close']) or currentData['low'] < self.upperbound:
                    self.Status == "RETESTED"

        #if down candles formed the FVG    
        else:
            if currentData['close'] > self.lowerbound:
                self.Status = "INVALID"
                return self.Status
            
            if self.Status == "VALID":
                if currentData['close'] < self.lowerbound and currentData['high'] > self.lowerbound:
                    self.Status = "TESTED"
                    if currentData['high'] > self.upperbound:
                        self.Status = "FULLYTESTED"
                    return self.Status
            if self.Status == "TESTED":
                if self.checkifinrange(currentData['close']) or currentData['high'] > self.lowerbound:
                    self.Status == "RETESTED"
            

    def checkifinrange(self, value):
        if value > self.lowerbound and value < self.upperbound:
            return True
        
        else:
            return False
            