import re

def extract_date(s, fallback_year):
    m = re.search(r'\b(\d{1,2})\s*[/.]\s*(\d{1,2})(?:\s*[/.]\s*(\d{2,4}))?\b', s)
    if not m: return None, s
    mo, da, yr = int(m.group(1)), int(m.group(2)), m.group(3)
    if yr:
        yr=int(yr); yr = yr+2000 if yr<100 else yr
    else:
        yr = fallback_year
    if not (yr and 1<=mo<=12 and 1<=da<=31):
        return None, s
    remainder = s[:m.start()] + ' ' + s[m.end():]
    return f"{yr:04d}-{mo:02d}-{da:02d}", remainder

def extract_time(s):
    # 1. Detect AND STRIP am/pm first so glued forms (4:27pm) don't contaminate digit parsing
    ampm=None
    low=s.lower()
    m=re.search(r'([ap])\.?\s*m\.?', low)
    if m:
        ampm=m.group(1)
        # remove the am/pm token from the working string (by index on lowercased == same length)
        s = s[:m.start()] + ' ' + s[m.end():]
    # also handle a trailing lone 'p' / 'a' (e.g. "5:32p")
    else:
        m2=re.search(r'(\d)\s*([ap])\b', low)
        if m2:
            ampm=m2.group(2)
            s = re.sub(r'([ap])\b','',s,flags=re.I)
    # 2. Now parse a clean H:MM (colon or dot), else military HHMM, else bare hour
    h=mnt=None
    mt=re.search(r'\b(\d{1,2})\s*[:.]\s*(\d{2})\b', s)   # require exactly 2 minute digits
    if not mt:
        mt=re.search(r'\b(\d{1,2})\s*[:.]\s*(\d{1})\b', s)  # tolerate single min digit (e.g. 8:0)
    if mt:
        h=int(mt.group(1)); mnt=int(mt.group(2))
    else:
        mil=re.search(r'\b(\d{3,4})\b', s)
        if mil:
            tok=mil.group(1); h=int(tok[:-2]); mnt=int(tok[-2:]); ampm=None
        else:
            bare=re.search(r'\b(\d{1,2})\b', s)
            if bare:
                h=int(bare.group(1)); mnt=0
    if h is None: return None
    if ampm=='p' and h!=12: h+=12
    if ampm=='a' and h==12: h=0
    if not (0<=h<=23 and 0<=mnt<=59): return None
    return f"{h:02d}:{mnt:02d}:00"

def normalize(v, resp_date):
    if v is None or v.strip()=='': return (None,'blank')
    s=v.strip(); low=s.lower()
    if 'did not' in low or 'no response' in low or 'no answer' in low or 'didnt' in low or low in ('na','n/a','none','-','duplicate','n','no','n.a','n.a.'):
        return (None,'non_time')
    # if multiple times separated by comma, keep the FIRST (earliest entered)
    s_first = s.split(',')[0] if (',' in s and not re.search(r'\d{1,2}/\d{1,2}', s.split(',')[0])) else s
    # actually: only split on comma if the part before comma already has a time and no date; safer to handle below
    s2=re.sub(r'\(.*?\)',' ',s).strip()
    fy=int(resp_date[:4]) if resp_date else None
    date_iso, remainder = extract_date(s2, fy)
    time_src = remainder if date_iso else s2
    time_iso = extract_time(time_src)
    if date_iso and time_iso: return (f"{date_iso}T{time_iso}",'full_parse')
    if time_iso and resp_date: return (f"{resp_date}T{time_iso}",'time_only_sysdate')
    if date_iso:               return (f"{date_iso}T00:00:00",'date_only_midnight')
    if resp_date:              return (f"{resp_date}T00:00:00",'fallback_sysdate')
    return (None,'no_date_available')
