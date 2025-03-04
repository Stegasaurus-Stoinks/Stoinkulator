import numpy as np
from scipy.signal import argrelextrema

def calculate_slope(x1,y1,x2,y2):
        slope = (y2-y1)/(x2-x1)
        return slope

def display_waves(possibleWaves, array = []):
    wavesfordisplay = []
    #print(array)
    if array == []:
        for wave in possibleWaves:
            waveplot = wave.assemble()
            #wave.printdata()
            wavesfordisplay.append(waveplot)

    else:
        for i in array:
            #print(i)
            waveplot = possibleWaves[i].assemble()
            #print(waveplot)
            wavesfordisplay.append(waveplot)

    return wavesfordisplay
            



    

def find_line(endX,wave_x,ilocs_minMax):
    if np.isnan(endX):
        ilocs_minMax_valid = [x for x in ilocs_minMax if x>wave_x]#all max's past point 1
    else:
        ilocs_minMax_valid = [x for x in ilocs_minMax if (x>wave_x and x<endX)]#max's in wave 1
    return ilocs_minMax_valid

#code to detect if a lower min exists between points given in args
"""
wavex and wavey = wave.x123 and wave.y123. Use the wave values preceding the wave you're checking

"""
def min_limit_rule_break(x, wavex, wavey, mins):
    new_list = [item for item in mins[wavex:x] if not(np.isnan(item))]
    ruleBreak = any(h < wavey for h in new_list)
    return ruleBreak

#code to detect if an upper min exists between points given in args
"""
wavex and wavey = wave.x123 and wave.y123. Use the wave values preceding the wave you're checking

"""
def max_limit_rule_break(x, wavex, wavey, maxs):
    new_list = [item for item in maxs[wavex:x] if not(np.isnan(item))]
    ruleBreak = any(h > wavey for h in new_list)
    return ruleBreak

#if there are more future points to check and wave is valid, then print incomplete wave
def check_future_points(x, ilocs_min_valid,reach,tradingWaves,wave):
    if ilocs_min_valid:
        if(x == ilocs_min_valid[-1] and reach > len(ilocs_min_valid)):
            tradingWaves.append(ElliotImpulse(wave.plotSize,wave.x1,wave.y1,wave.x2,wave.y2,wave.x3,wave.y3,wave.x4,wave.y4,wave.x5,wave.y5,wave.x6,wave.y6))
    
def check_retracement(num1,num2,num3,retList):
    reach = 0.03
    properRetracement = False

    ans = (num2-num3)/(num2-num1)
    for retrace in retList:
        lowLimit = retrace-reach
        highLimit = retrace+reach
        if ans > lowLimit and ans < highLimit:
            return True
    return properRetracement

############ Big boy function. father of all functions. Tamper with if you dare. A single wrong change will cause a cataclysmic chain of events

def elliot_recursive_blast(backtest,plotSize,n,startX=np.NaN,endX=np.NaN,level=0):
    # Uncomment this for debugging recursive stuff
    #print("level: ",level)
    #Calculating mins and maxs
    #print("startX:",startX)
    #print("endX:",endX)
    #print("BEFORE RODER: ",n)

    global seg1top
    
    if np.isnan(startX): #if this is a fresh blast
        o = n #then set order = base order
    else: #else, we are in a recursive blast
        o = round(n/3) #Adjust this to add more or less mins and maxs (2 was the best one I found for short term)
        if o == 0:
            return list()
        #print("CURRENT RODER: ",o)
    
    ilocs_min = argrelextrema(backtest.low.values, np.less_equal, order=o)[0]
    ilocs_max = argrelextrema(backtest.high.values, np.greater_equal, order=o)[0]
    print(ilocs_min)
    #print(ilocs_max)

    #array of min and max plotpoints
    #fill array with nan's first, then replace nan's with min and max values where necessary

    mins = [np.NaN] * plotSize
    for i in range (0,len(ilocs_min)):
        if ilocs_min[i] < len(mins):
            mins[ilocs_min[i]] = backtest.iloc[ilocs_min[i]].low * 0.9999

    maxs = [np.NaN] * plotSize
    for i in range (0,len(ilocs_max)):
        if ilocs_max[i] < len(maxs):
            maxs[ilocs_max[i]] = backtest.iloc[ilocs_max[i]].high * 1.0001

    

    reach = 3
    finishedWaves = list()
    tradingWaves = list()

    #for every min in chart
    for i in range (0,len(ilocs_min)):
        if(1):
        #try:
            #temp block checks if we are in a recursive function and already have a startX. We only want to be checking elliots with that startX
            temp = False
            if not np.isnan(startX):
                if ilocs_min[i] != startX:
                    temp = True
            if temp is True:
                continue
            wave = ElliotImpulse(plotSize)
            wave.x1 = ilocs_min[i]
            wave.y1 = mins[ilocs_min[i]]
            
            #checking wave 1/point 2 [ / ]
            ilocs_max_valid = findLine(endX,wave.x1,ilocs_max)
            for curPoint in ilocs_max_valid[0:reach+1]:
                if(wave.checkpoint2(curPoint,maxs[curPoint], mins)):
                    wave.x2 = curPoint
                    wave.y2 = maxs[curPoint]

                    #checking wave 2/point 3 [ /\ ]
                    ilocs_min_valid = findLine(endX,wave.x2,ilocs_min)
                    for curPoint in ilocs_min_valid[0:reach+1]:
                        if(wave.checkpoint3(curPoint,mins[curPoint], maxs,mins)):
                            wave.x3 = curPoint
                            wave.y3 = mins[curPoint]
                            
                            checkFuturePoints(curPoint, ilocs_min_valid,reach,tradingWaves,wave)
                            
                            #checking wave 3/point 4 [ /\/ ]
                            ilocs_max_valid = findLine(endX,wave.x3,ilocs_max)
                            for curPoint in ilocs_max_valid[0:reach+1]:
                                if(wave.checkpoint4(curPoint,maxs[curPoint], mins)):
                                    wave.x4 = curPoint
                                    wave.y4 = maxs[curPoint]

                                    checkFuturePoints(curPoint, ilocs_max_valid,reach,tradingWaves,wave)

                                    #checking wave 4/point 5 [ /\/\ ]
                                    ilocs_min_valid = findLine(endX,wave.x4,ilocs_min)
                                    for curPoint in ilocs_min_valid[0:reach+1]:
                                        if(wave.checkpoint5(curPoint,mins[curPoint],maxs)):
                                            wave.x5 = curPoint
                                            wave.y5 = mins[curPoint]
                                            
                                            checkFuturePoints(curPoint, ilocs_min_valid,reach,tradingWaves,wave)

                                            #checking wave 5/point 6 [ /\/\/ ]
                                            ilocs_max_valid = findLine(endX,wave.x5,ilocs_max)
                                            for curPoint in ilocs_max_valid[0:reach+1]:
                                                if(wave.checkpoint6(curPoint,maxs[curPoint],mins)):
                                                    wave.x6 = curPoint
                                                    wave.y6 = maxs[curPoint]
                                                    finishedWaves.append(ElliotImpulse(wave.plotSize,wave.x1,wave.y1,wave.x2,wave.y2,wave.x3,wave.y3,wave.x4,wave.y4,wave.x5,wave.y5,wave.x6,wave.y6))
                                                        
                                                    # possWaves1 = elliotRecursiveBlast(backtest,plotSize,o,wave.x1,wave.x2,level+1)
                                                    # if possWaves1 != []:
                                                    #     possibleWaves.extend(possWaves1)
                                                    # possWaves3 = elliotRecursiveBlast(backtest,plotSize,o,wave.x3,wave.x4,level+1)
                                                    # if possWaves1 != []:
                                                    #     possibleWaves.extend(possWaves3)
                                                    
                                                    #waveplot = wave.assemble()
                                                    #print(wave.printdata())
                                                    #print(waveplot)
                                                    #extraplots.append(plotting.make_addplot(waveplot,ax=ax1))        
        
        else:
        #except:       
            print("something broke in the try thingy")
    return finishedWaves,tradingWaves
