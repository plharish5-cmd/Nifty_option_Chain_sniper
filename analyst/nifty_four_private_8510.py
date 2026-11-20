"""Private authorized browser-request capture. Parses only; no request is executed.
Endpoint role, IDs, response decoding and instrument selection remain UNVERIFIED.
"""
import argparse
import json
import shlex
import sys
import termios
from pathlib import Path
from analyst.stockmojo_downloader_8510 import Downloader,save_credentials,atomic
from analyst.nifty_four_oi_8510 import rules

ALLOWED={'https://data.stockmojo.in/gc/data','https://z.stockmojo.in/oi'}


def parse_chart_request(text):
    try:parts=shlex.split(text.replace('\\\n',' '))
    except ValueError:raise ValueError('INVALID_COPIED_CURL') from None
    urls=[x for x in parts if x in ALLOWED]
    if len(urls)!=1 or not parts or parts[0]!='curl':raise ValueError('AUTHORIZED_CHART_ENDPOINT_REQUIRED')
    headers={};bodies=[];method=None
    for i,part in enumerate(parts[:-1]):
        if part in ('-H','--header'):
            key,sep,value=parts[i+1].partition(':');key=key.strip().lower()
            if sep and key in ('j','t'):
                if key in headers and headers[key]!=value.strip():raise ValueError('CONFLICTING_AUTH_HEADERS')
                headers[key]=value.strip()
        elif part in ('--data-raw','--data','--data-binary','-d'):bodies.append(parts[i+1])
        elif part in ('-X','--request'):method=parts[i+1].upper()
    if len(bodies)>1 or method not in (None,'GET','POST'):raise ValueError('UNSUPPORTED_REQUEST_SHAPE')
    if any(not headers.get(k) or '\n' in headers[k] or '\r' in headers[k] for k in ('j','t')):raise ValueError('MISSING_J_T_AUTH_HEADERS')
    return {'STOCKMOJO_JWT_J':headers['j'],'STOCKMOJO_TOKEN_T':headers['t'],
        'STOCKMOJO_FOUR_REQUEST':json.dumps({'endpoint':urls[0],'method':method or ('POST' if bodies else 'GET'),'body':bodies[0] if bodies else None},separators=(',',':'))}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]));args=parser.parse_args()
    if not sys.stdin.isatty():parser.exit(2,'Interactive private terminal required.\n')
    print('No Google password is needed. Paste Copy as cURL (bash) from the selected NIFTY chart request. Input is hidden. Enter, END, Enter.',flush=True)
    fd=sys.stdin.fileno();old=termios.tcgetattr(fd);hidden=list(old);hidden[3]&=~termios.ECHO;lines=[];size=0
    try:
        termios.tcsetattr(fd,termios.TCSADRAIN,hidden)
        for line in sys.stdin:
            if line.strip()=='END':break
            size+=len(line.encode())
            if size>rules()['max_private_input_bytes']:raise ValueError('REQUEST_TOO_LARGE')
            lines.append(line)
        values=parse_chart_request(''.join(lines))
    except ValueError as e:parser.exit(2,str(e)+'; nothing configured.\n')
    finally:termios.tcsetattr(fd,termios.TCSADRAIN,old);print('',flush=True)
    d=Downloader(args.root)
    try:
        d.acquire()
        if (Path(args.root)/'.env').is_symlink():raise ValueError('Symlink credential file rejected')
        save_credentials(args.root,values)
        status={'endpoint':json.loads(values['STOCKMOJO_FOUR_REQUEST'])['endpoint'],'selection_status':'UNVERIFIED_NIFTY_REQUEST','acquisition':'BLOCKED_AUTHORIZED_DECODING_AND_RESPONSE_MAPPING_REQUIRED','network_requests':0}
        atomic(Path(args.root)/'data/stockmojo_four_oi/request_status.json',json.dumps(status,indent=2).encode())
        print('Private request saved, unrelated .env entries preserved, mode0600. No request executed; selection and decoding remain unverified.')
    except (OSError,ValueError):parser.exit(2,'Capture could not finish; no private values logged. Stop only the downloader before retrying.\n')
    finally:d.close()
if __name__=='__main__':main()
