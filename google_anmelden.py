import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


PAIRING_B64 = '__PAIRING_CONFIGURATION__'


def main():
    if PAIRING_B64.startswith('__'):
        raise SystemExit('Bitte den Anmeldehelfer direkt aus deiner Backup-App herunterladen.')
    config=json.loads(base64.urlsafe_b64decode(PAIRING_B64))
    state=secrets.token_urlsafe(32)
    verifier=secrets.token_urlsafe(64)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    result={}
    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            values=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if not secrets.compare_digest(values.get('state',[''])[0],state):
                self.send_response(400); self.end_headers(); self.wfile.write(b'Ungueltige Anmeldung.'); return
            if values.get('error'):
                result['error']=values['error'][0]
            else:
                result['code']=values.get('code',[''])[0]
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers()
            self.wfile.write('<meta charset="utf-8"><h2>Google-Freigabe empfangen</h2><p>Bitte zum Anmeldehelfer zurückkehren. Er verbindet jetzt die Backup-App.</p>'.encode())
        def log_message(self,*args):
            pass
    server=HTTPServer(('127.0.0.1',0),Callback)
    server.timeout=1
    redirect=f'http://127.0.0.1:{server.server_port}'
    params={'client_id':config['client_id'],'redirect_uri':redirect,'response_type':'code',
            'scope':' '.join(config['scopes']),'state':state,'code_challenge':challenge,
            'code_challenge_method':'S256','access_type':'offline','prompt':'consent select_account'}
    link='https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode(params)
    print('Google-Anmeldung für '+{'drive':'Drive','photos':'Fotos','monitoring':'die Verbrauchsanzeige'}[config['purpose']])
    print('Im Browser das Konto mit dem gewünschten Speicher auswählen. Dein Passwort wird nur bei Google eingegeben.')
    webbrowser.open(link)
    deadline=time.time()+1200
    try:
        while not result and time.time()<deadline:
            server.handle_request()
    finally:
        server.server_close()
    if result.get('error') or not result.get('code'):
        raise SystemExit('Anmeldung abgebrochen oder abgelaufen. Bitte in der App erneut starten.')
    body=urllib.parse.urlencode({'client_id':config['client_id'],'client_secret':config['client_secret'],
         'code':result['code'],'redirect_uri':redirect,'code_verifier':verifier,'grant_type':'authorization_code'}).encode()
    try:
        request=urllib.request.Request('https://oauth2.googleapis.com/token',data=body)
        with urllib.request.urlopen(request,timeout=30) as response:
            tokens=json.load(response)
        if not tokens.get('refresh_token'):
            raise SystemExit('Google hat keine dauerhafte Freigabe geliefert. Anmeldung in der App erneut starten.')
        payload=json.dumps({'flow_id':config['flow_id'],'pairing_token':config['pairing_token'],'tokens':tokens}).encode()
        request=urllib.request.Request(config['base_url'].rstrip('/')+'/api/oauth/finish',data=payload,
                                      headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=45) as response:
            connected=json.load(response)
        print('\nErfolgreich verbunden: '+connected['email'])
        print('Du kannst zur Backup-App zurückkehren und diese heruntergeladene Datei löschen.')
    except urllib.error.HTTPError as error:
        if 'googleapis.com' in error.url:
            raise SystemExit('Google hat die Anmeldung nicht akzeptiert. Client-Datei und Projektfreigabe prüfen.') from None
        try:
            message=json.loads(error.read()).get('error','Verbindung zur Backup-App fehlgeschlagen.')
        except Exception:
            message='Verbindung zur Backup-App fehlgeschlagen.'
        raise SystemExit(message) from None
    except urllib.error.URLError:
        raise SystemExit('Die Backup-App oder Google ist nicht erreichbar. Bitte erneut versuchen.') from None


if __name__=='__main__':
    try:
        main()
    finally:
        if sys.stdin.isatty():
            input('\nEnter zum Schließen …')
