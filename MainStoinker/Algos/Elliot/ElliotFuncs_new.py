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