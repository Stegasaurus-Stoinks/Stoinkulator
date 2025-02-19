# FVG CLASS

class FVG:
    # States:
    # VALID
    # TESTED (first test: must close outside of FVG, low must be in FVG)
    # RETESTED  (second test: must close in or low must be in FVG)
    # FULLYTESTED  (low of candle fully swept range of FVG)
    # INVALID   (FVG was invalidated by closing outside of the range on the wrong side)

    def __init__(self, upperbound, lowerbound, time, direction):
        self.upperbound = upperbound
        self.lowerbound = lowerbound
        self.startTime = time
        self.direction = direction
        self.Status = "VALID"

    def updateFVG(self, currentData):
        #if up candles formed the FVG
        if self.direction:
            if currentData['close'] < self.lowerbound:
                self.Status = "INVALID"
                return
                #TODO add timeout section to the update
            
            if self.Status == "VALID":
                if currentData['close'] > self.upperbound and currentData['low'] < self.upperbound:
                    self.Status = "TESTED"
                    if currentData['low'] < self.lowerbound:
                        self.Status = "FULLYTESTED"
                    return
                
            if self.Status == "TESTED":
                if self.checkifinrange(currentData['close']) or currentData['low'] < self.upperbound:
                    self.Status == "RETESTED"

        #if down candles formed the FVG    
        else:
            if currentData['close'] > self.lowerbound:
                self.Status = "INVALID"
                return
            
            if self.Status == "VALID":
                if currentData['close'] < self.lowerbound and currentData['high'] > self.lowerbound:
                    self.Status = "TESTED"
                    if currentData['high'] > self.upperbound:
                        self.Status = "FULLYTESTED"
                    return
            if self.Status == "TESTED":
                if self.checkifinrange(currentData['close']) or currentData['high'] > self.lowerbound:
                    self.Status == "RETESTED"

        

                    

    def checkifinrange(self, value):
        if value > self.lowerbound and value < self.upperbound:
            return True
        
        else:
            return False