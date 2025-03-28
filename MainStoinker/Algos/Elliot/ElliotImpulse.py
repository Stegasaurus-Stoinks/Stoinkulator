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


    def __init__(self, plotSize, time_1=np.NaN, price_1=np.NaN, time_2=np.NaN, price_2=np.NaN, time_3= np.NaN, price_3=np.NaN, time_4=np.NaN, price_4=np.NaN, time_5=np.NaN, price_5=np.NaN, time_6=np.NaN, price_6=np.NaN):

        self.plotSize = plotSize
        #CheckyBouncyLimitOfMostRecentPointyFoRealsy is the validation limit for the most recent min/max
        # in other words, we use this to make sure the price is going up before declaring a min
        self.CheckyBouncyLimitOfMostRecentPointyFoRealsy = plotSize - 3
        self.time_1 = time_1
        self.price_1 = price_1
        self.time_2 = time_2
        self.price_2 = price_2
        self.time_3 = time_3
        self.price_3 = price_3
        self.time_4 = time_4
        self.price_4 = price_4
        self.time_5 = time_5
        self.price_5 = price_5 
        self.time_6 = time_6
        self.price_6 = price_6

    def printdata(self):
        print(self.time_1,self.price_1,self.time_2,self.price_2,self.time_3,self.price_3,self.time_4,self.price_4,self.time_5,self.price_5,self.time_6,self.price_6)

    def assemble(self,print=False):
        #calculate slopes between each line and create a plotable line

        #check to see if all the values have been defined
        #NEED TO CHANGE THIS TO TRIGGER ON BEING NANS IF I WANT IT TO WORK?
        if(self.price_1 == 0 or self.price_2 == 0 or self.price_3 == 0 or self.price_4 == 0 or self.price_5 == 0 or self.price_5 == 0):
            print("Error! Could not assemble the Elliot wave due to missing/undefined data")
            print("Y1:{} Y2:{} Y3:{} Y4:{} Y5:{} Y6:{}".format(self.price_1,self.price_2,self.price_3,self.price_4,self.price_5,self.price_6))
            return()

        if(print):
            print("Assembling the Elliot wave with the given parameters")
            self.printdata()

        self.slope1 = ElliotFuncs.calculate_slope(self.time_1, self.price_1, self.time_2, self.price_2)
        self.slope2 = ElliotFuncs.calculate_slope(self.time_2, self.price_2, self.time_3, self.price_3)
        self.slope3 = ElliotFuncs.calculate_slope(self.time_3, self.price_3, self.time_4, self.price_4)
        self.slope4 = ElliotFuncs.calculate_slope(self.time_4, self.price_4, self.time_5, self.price_5)
        self.slope5 = ElliotFuncs.calculate_slope(self.time_5, self.price_5, self.time_6, self.price_6)

        #print(self.slope1,self.slope2,self.slope3,self.slope4,self.slope5)
        wave = [np.NaN] * self.plotSize
        try:
            x = 0
            for k in range(self.time_1, self.time_2+1):
                wave[k] = float(x*self.slope1) + self.price_1
                x += 1

            x = 0
            for k in range(self.time_2, self.time_3+1):
                wave[k] = float(x*self.slope2) + self.price_2
                x += 1

            x = 0
            for k in range(self.time_3, self.time_4+1):
                wave[k] = float(x*self.slope3) + self.price_3
                x += 1

            x = 0
            for k in range(self.time_4, self.time_5+1):
                wave[k] = float(x*self.slope4) + self.price_4
                x += 1

            x = 0
            for k in range(self.time_5, self.time_6+1):
                wave[k] = float(x*self.slope5) + self.price_5
                x += 1
        except:
            return(wave)

        return(wave)

    
    def checkpoint2(self,x2,y2,mins):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x2>self.time_1 and y2>self.price_1:
            if not ElliotFuncs.min_limit_rule_break(x2, self.time_1, self.price_1, mins):#check if max exists between points 2 and 3
                result = True
        return result


    def checkpoint3(self,x3,y3,maxs,mins):
        retList = [.50, .618, .65, .786, .886]
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x3 < self.CheckyBouncyLimitOfMostRecentPointyFoRealsy:
            if x3>self.time_2 and y3>self.price_1 and y3 < self.price_2:
                if not ElliotFuncs.max_limit_rule_break(x3, self.time_1, self.price_2, maxs):#check if max exists between points 1 and 3
                    if not ElliotFuncs.min_limit_rule_break(x3, self.time_1, self.price_1, mins):#check if max exists between points 2 and 3
                        if ElliotFuncs.check_retracement(self.price_1,self.price_2,y3,retList):
                            result = True
                    
        return result

    def checkpoint4(self,x4,y4,mins):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x4>self.time_3 and y4>self.price_2 and y4 > self.price_3:
            if not ElliotFuncs.min_limit_rule_break(x4, self.time_3, self.price_3, mins):#check if min exists between points 3 and 4
                result = True

        return result

    def checkpoint5(self,x5,y5,maxs):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x5 < self.CheckyBouncyLimitOfMostRecentPointyFoRealsy:
            if x5>self.time_4 and y5>self.price_3 and y5<self.price_4:
                if not ElliotFuncs.max_limit_rule_break(x5, self.time_4, self.price_4, maxs):#check if max exists between points 4 and 5
                    result = True

        return result

    def checkpoint6(self,x6,y6,mins):
        result = False
        #add rules and conditions that would make this point work in the elliot wave
        if x6>self.time_5 and y6>self.price_4 and y6>self.price_5:
            if not ElliotFuncs.min_limit_rule_break(x6, self.time_5, self.price_5, mins):#check if min exists between points 5 and 6
                result = True

        return result


    



    def definepoints(self,x1,y1,x2,y2,x3,y3,x4,y4,x5,y5,x6,y6):
        self.time_1 = x1
        self.price_1 = y1
        self.time_2 = x2
        self.price_2 = y2
        self.time_3 = x3
        self.price_3 = y3
        self.time_4 = x4
        self.price_4 = y4
        self.time_5 = x5
        self.price_5 = y5
        self.time_6 = x6
        self.price_6 = y6

    def clear(self):
        self.time_1 = np.NaN
        self.price_1 = np.NaN
        self.time_2 = np.NaN
        self.price_2 = np.NaN
        self.time_3 = np.NaN
        self.price_3 = np.NaN
        self.time_4 = np.NaN
        self.price_4 = np.NaN
        self.time_5 = np.NaN
        self.price_5 = np.NaN
        self.time_6 = np.NaN
        self.price_6 = np.NaN

        
    def score():
        print("Give the Elliot Wave a score of certainty based on how many rules it followed")