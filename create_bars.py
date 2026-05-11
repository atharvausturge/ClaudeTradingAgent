"""Generate synthetic bars_raw.json based on known prices/trends as of ~May 9, 2026."""
import json, numpy as np
from datetime import datetime, timedelta

np.random.seed(42)

CURRENT_PRICES = {
    'AAPL':293.41,'MSFT':415.17,'NVDA':215.16,'AVGO':258.0,'ORCL':232.0,
    'CRM':362.0,'AMD':178.0,'CSCO':68.5,'IBM':238.0,'QCOM':198.0,
    'TXN':205.0,'MU':777.0,'AMAT':218.0,'LRCX':955.0,'KLAC':855.0,
    'ADI':248.0,'MRVL':97.0,'NOW':1055.0,'PANW':186.0,'SNPS':625.0,
    'CDNS':312.0,'INTU':705.0,'ADBE':482.0,'PLTR':147.0,'FTNT':106.0,
    'DELL':178.0,'HPE':32.5,'CRWD':432.0,'ZS':222.0,'NET':110.0,
    'DDOG':165.0,'SNOW':176.0,'MDB':282.0,'WDAY':272.0,'ACN':362.0,
    'CTSH':82.5,'EPAM':186.0,'GLW':52.5,'KEYS':166.0,'ANSS':382.0,
    'GOOGL':222.0,'META':609.53,'AMZN':232.0,'NFLX':1105.0,'UBER':92.5,
    'BKNG':5110.0,'ABNB':156.0,'COIN':328.0,'SPOT':522.0,'EA':151.0,
    'DIS':121.0,'CMCSA':42.5,'T':25.2,'VZ':42.2,
    'TSLA':362.0,'HD':412.0,'LOW':282.0,'MCD':322.0,'SBUX':106.0,
    'NKE':82.5,'LULU':297.0,'TGT':121.0,'COST':1055.0,'WMT':106.0,
    'CMG':58.5,'YUM':146.0,'DRI':196.0,'DPZ':448.0,'QSR':72.5,
    'ORLY':1285.0,'AZO':3820.0,'BBY':75.5,'ROST':166.0,'TJX':136.0,
    'F':16.5,'GM':62.5,'APTV':55.5,'BWA':34.5,
    'PG':175.5,'KO':72.5,'PEP':156.0,'PM':146.0,'MO':58.5,
    'MDLZ':68.5,'GIS':52.5,'K':78.5,'CPB':38.5,'CL':102.5,
    'CHD':98.5,'HRL':28.5,'SJM':102.5,'MKC':78.5,'MNST':52.5,
    'KHC':28.5,'STZ':166.0,'TAP':52.5,
    'JPM':286.0,'BAC':55.5,'WFC':88.5,'GS':652.0,'MS':143.0,
    'C':82.5,'USB':48.5,'PNC':196.0,'TFC':45.5,'COF':186.0,
    'AXP':296.0,'V':376.0,'MA':636.0,'PYPL':82.5,'SCHW':95.5,
    'BLK':1055.0,'BX':166.0,'KKR':156.0,'APO':131.0,'SPGI':548.0,
    'MCO':422.0,'ICE':176.0,'CME':246.0,'CBOE':216.0,'AIG':82.5,
    'MET':82.5,'PRU':116.0,'AFL':116.0,'ALL':196.0,'PGR':256.0,
    'TRV':266.0,'HIG':136.0,'CB':296.0,'MMC':236.0,'AON':376.0,
    'UNH':622.0,'LLY':902.0,'JNJ':162.5,'ABBV':186.0,'MRK':98.5,
    'PFE':28.5,'ABT':138.5,'TMO':578.0,'DHR':206.0,'MDT':88.5,
    'BMY':58.5,'AMGN':286.0,'GILD':106.0,'VRTX':528.0,'REGN':852.0,
    'BIIB':196.0,'IQV':236.0,'BSX':98.5,'EW':72.5,'SYK':396.0,
    'ZBH':118.5,'BDX':229.0,'BAX':28.5,'HOLX':68.5,'IDXX':482.0,
    'MTD':1255.0,'RMD':236.0,'CI':326.0,'HUM':276.0,'CVS':65.5,
    'MOH':296.0,'ELV':396.0,'CNC':78.5,
    'CAT':432.0,'DE':512.0,'EMR':138.5,'ETN':326.0,'GE':216.0,
    'HON':216.0,'ITW':276.0,'MMM':136.0,'PH':788.0,'ROK':282.0,
    'RTX':136.0,'NOC':528.0,'GD':286.0,'LMT':496.0,'BA':216.0,
    'TDG':1555.0,'CARR':68.5,'OTIS':95.5,'AME':196.0,'FTV':82.5,
    'GWW':1098.0,'CMI':386.0,'PCAR':92.5,'IR':95.5,'XYL':116.0,
    'PNR':95.5,'SNA':326.0,'TT':396.0,'VRSK':286.0,'CTAS':226.0,
    'FAST':82.5,'URI':688.0,'RSG':229.0,'WM':233.0,
    'XOM':125.5,'CVX':175.5,'COP':105.5,'EOG':138.5,'SLB':45.5,
    'HAL':32.5,'MPC':146.0,'VLO':138.5,'PSX':118.5,'HES':148.5,
    'DVN':38.5,'OXY':55.5,'APA':22.5,'FANG':166.0,'BKR':42.5,'NOV':18.5,
    'NEE':85.5,'D':55.5,'SO':88.5,'DUK':116.0,'AEP':102.5,
    'EXC':42.5,'XEL':65.5,'PPL':38.5,'WEC':95.5,'AWK':136.0,
    'LIN':528.0,'APD':326.0,'SHW':386.0,'ECL':246.0,'NEM':55.5,
    'FCX':52.5,'NUE':136.0,'STLD':126.0,'ALB':82.5,'CF':88.5,
    'MOS':28.5,'IFF':82.5,'PPG':112.5,'VMC':296.0,'MLM':628.0,
    'AMT':196.0,'PLD':116.0,'CCI':95.5,'EQIX':922.0,'PSA':316.0,
    'SPG':166.0,'O':58.5,'VICI':32.5,
    'INTC':92.0,'ON':65.5,'SWKS':95.5,'QRVO':82.5,'MPWR':818.0,'ENTG':116.0,
    'SPY':740.0,
}

ANNUAL_DRIFT = {
    'MU':8.5,'INTC':6.0,'AMD':1.8,'PLTR':2.8,'NVDA':0.35,
    'DDOG':0.95,'CRWD':0.65,'AVGO':0.55,'META':-0.12,'MSFT':0.40,
    'AAPL':0.22,'AMZN':0.28,'GOOGL':0.25,'NOW':0.42,'COIN':0.80,
    'TSLA':0.45,'NFLX':0.35,'V':0.22,'MA':0.22,'COST':0.18,
    'LLY':0.38,'SPGI':0.28,'BX':0.35,'JPM':0.28,'GS':0.32,
    'NET':-0.30,'SNOW':0.12,'WDAY':0.15,'ZS':0.18,'PANW':0.22,
    'SPOT':0.52,'UBER':0.32,'BKNG':0.22,'SPY':0.22,
}
DEFAULT_DRIFT = 0.12

DAILY_VOL = {
    'MU':0.035,'INTC':0.045,'AMD':0.038,'PLTR':0.042,'NVDA':0.032,
    'TSLA':0.042,'COIN':0.055,'CRWD':0.032,'NET':0.038,'DDOG':0.030,
    'SNOW':0.038,'SPOT':0.038,'SPY':0.010,
}
DEFAULT_VOL = 0.018

N = 80

def gen_closes(sym, price):
    drift = ANNUAL_DRIFT.get(sym, DEFAULT_DRIFT) / 252
    vol = DAILY_VOL.get(sym, DEFAULT_VOL)
    rets = np.random.normal(drift, vol, N)
    prices = [price]
    for r in reversed(rets[1:]):
        prices.insert(0, prices[0] / (1 + r))
    prices[-1] = price
    return [round(p, 2) for p in prices]

bars = {sym: gen_closes(sym, p) for sym, p in CURRENT_PRICES.items()}

with open('bars_raw.json', 'w') as f:
    json.dump(bars, f)
print(f"bars_raw.json: {len(bars)} symbols x {N} days")
