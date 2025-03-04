import numpy as np
from scipy.signal import argrelextrema

from MainStoinker.Algos.Elliot import ElliotFuncs_new as ElliotFuncs











class ElliotImpulse(object):

    # |              6
    # |         4   /
    # |    2   / \ /
    # |   / \ /   5 
    # |  /   3
    # | 1
    # |________________


    def __init__(self, plotSize,x1= np.NaN,y1= np.NaN,x2= np.NaN,y2= np.NaN,x3= np.NaN,y3= np.NaN,x4= np.NaN,y4= np.NaN,x5= np.NaN,y5= np.NaN,x6= np.NaN,y6= np.NaN):

        self.plotSize = plotSize
        #CheckyBouncyLimitOfMostRecentPointyFoRealsy is the validation limit for the most recent min/max
        # in other words, we use this to make sure the price is going up before declaring a min
        self.CheckyBouncyLimitOfMostRecentPointyFoRealsy = plotSize - 3
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.x3 = x3
        self.y3 = y3
        self.x4 = x4
        self.y4 = y4
        self.x5 = x5
        self.y5 = y5 
        self.x6 = x6
        self.y6 = y6

    def printdata(self):
        print(self.x1,self.y1,self.x2,self.y2,self.x3,self.y3,self.x4,self.y4,self.x5,self.y5,self.x6,self.y6)

    def assemble(self,print=False):
        #calculate slopes between each line and create a plotable line

        #check to see if all the values have been defined
        #NEED TO CHANGE THIS TO TRIGGER ON BEING NANS IF I WANT IT TO WORK?
        if(self.y1 == 0 or self.y2 == 0 or self.y3 == 0 or self.y4 == 0 or self.y5 == 0 or self.y5 == 0):
            print("Error! Could not assemble the Elliot wave due to missing/undefined data")
            print("Y1:{} Y2:{} Y3:{} Y4:{} Y5:{} Y6:{}".format(self.y1,self.y2,self.y3,self.y4,self.y5,self.y6))
            return()

        if(print):
            print("Assembling the Elliot wave with the given parameters")
            self.printdata()

        self.slope1 = ElliotFuncs.calculate_slope(self.x1, self.y1, self.x2, self.y2)
        self.slope2 = ElliotFuncs.calculate_slope(self.x2, self.y2, self.x3, self.y3)
        self.slope3 = ElliotFuncs.calculate_slope(self.x3, self.y3, self.x4, self.y4)
        self.slope4 = ElliotFuncs.calculate_slope(self.x4, self.y4, self.x5, self.y5)
        self.slope5 = ElliotFuncs.calculate_slope(self.x5, self.y5, self.x6, self.y6)

        #print(self.slope1,self.slope2,self.slope3,self.slope4,self.slope5)
        wave = [np.NaN] * self.plotSize
        try:
            x = 0
            for k in range(self.x1, self.x2+1):
                wave[k] = float(x*self.slope1) + self.y1
                x += 1

            x = 0
            for k in range(self.x2, self.x3+1):
                wave[k] = float(x*self.slope2) + self.y2
                x += 1

            x = 0
            for k in range(self.x3, self.x4+1):
                wave[k] = float(x*self.slope3) + self.y3
                x += 1

            x = 0
            for k in range(self.x4, self.x5+1):
                wave[k] = float(x*self.slope4) + self.y4
                x += 1

            x = 0
            for k in range(self.x5, self.x6+1):
                wave[k] = float(x*self.slope5) + self.y5
                x += 1
        except:
            return(wave)

        return(wave)

    
    def checkpoint2(self,x2,y2,mins):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x2>self.x1 and y2>self.y1:
            if not ElliotFuncs.min_limit_rule_break(x2, self.x1, self.y1, mins):#check if max exists between points 2 and 3
                result = True
        return result


    def checkpoint3(self,x3,y3,maxs,mins):
        retList = [.50, .618, .65, .786, .886]
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x3 < self.CheckyBouncyLimitOfMostRecentPointyFoRealsy:
            if x3>self.x2 and y3>self.y1 and y3 < self.y2:
                if not ElliotFuncs.max_limit_rule_break(x3, self.x1, self.y2, maxs):#check if max exists between points 1 and 3
                    if not ElliotFuncs.min_limit_rule_break(x3, self.x1, self.y1, mins):#check if max exists between points 2 and 3
                        if ElliotFuncs.check_retracement(self.y1,self.y2,y3,retList):
                            result = True
                    
        return result

    def checkpoint4(self,x4,y4,mins):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x4>self.x3 and y4>self.y2 and y4 > self.y3:
            if not ElliotFuncs.min_limit_rule_break(x4, self.x3, self.y3, mins):#check if min exists between points 3 and 4
                result = True

        return result

    def checkpoint5(self,x5,y5,maxs):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x5 < self.CheckyBouncyLimitOfMostRecentPointyFoRealsy:
            if x5>self.x4 and y5>self.y3 and y5<self.y4:
                if not ElliotFuncs.max_limit_rule_break(x5, self.x4, self.y4, maxs):#check if max exists between points 4 and 5
                    result = True

        return result

    def checkpoint6(self,x6,y6,mins):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x6>self.x5 and y6>self.y4 and y6>self.y5:
            if not ElliotFuncs.min_limit_rule_break(x6, self.x5, self.y5, mins):#check if min exists between points 5 and 6
                result = True

        return result


    



    def definepoints(self,x1,y1,x2,y2,x3,y3,x4,y4,x5,y5,x6,y6):
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.x3 = x3
        self.y3 = y3
        self.x4 = x4
        self.y4 = y4
        self.x5 = x5
        self.y5 = y5
        self.x6 = x6
        self.y6 = y6

    def clear(self):
        self.x1 = np.NaN
        self.y1 = np.NaN
        self.x2 = np.NaN
        self.y2 = np.NaN
        self.x3 = np.NaN
        self.y3 = np.NaN
        self.x4 = np.NaN
        self.y4 = np.NaN
        self.x5 = np.NaN
        self.y5 = np.NaN
        self.x6 = np.NaN
        self.y6 = np.NaN

        
    def score():
        print("Give the Elliot Wave a score of certainty based on how many rules it followed")