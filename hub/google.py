import datetime as dt
import hashlib
import json
import secrets
import threading
import time
from urllib.parse import urlparse

import requests

from .core import now

SCOPES = {
    'drive': ['openid', 'email', 'https://www.googleapis.com/auth/drive.file'],
    'photos': ['openid', 'email', 'https://www.googleapis.com/auth/photoslibrary.appendonly',
               'https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata'],
    'monitoring': ['openid','email','https://www.googleapis.com/auth/monitoring.read',
                   'https://www.googleapis.com/auth/cloud-platform.read-only'],
}


class GoogleError(RuntimeError):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Google:
    def __init__(self, store):
        self.store = store
        self.locks = {purpose:threading.RLock() for purpose in SCOPES}

    def configure_client(self, document):
        if not isinstance(document,dict) or 'installed' not in document:
            raise ValueError('Bitte eine Google-OAuth-JSON-Datei vom Typ Desktop-App auswählen.')
        value = document['installed']
        client_id, secret = value.get('client_id',''),value.get('client_secret','')
        if not client_id.endswith('.apps.googleusercontent.com') or len(client_id)>200 or not secret or len(secret)>300 or any(ord(c)<32 for c in client_id+secret):
            raise ValueError('Client-ID oder Client-Secret fehlt in der JSON-Datei.')
        previous = self.store.secrets().get('google_client',{})
        if previous and previous.get('client_id')!=client_id and any(self.store.secrets().get(x+'_oauth') for x in SCOPES):
            raise ValueError('Der Google-Client ist bereits verbunden. Zuerst beide Google-Verbindungen trennen.')
        self.store.update_secrets(google_client={'client_id':client_id,'client_secret':secret,
                                                'project_id':value.get('project_id','')})

    def new_flow(self, purpose):
        if purpose not in SCOPES:
            raise ValueError('Unbekannte Google-Verbindung.')
        client = self.store.secrets().get('google_client')
        if not client:
            raise ValueError('Zuerst die Google-Client-JSON-Datei einrichten.')
        flow_id, token = secrets.token_urlsafe(18),secrets.token_urlsafe(32)
        with self.store.db() as db:
            db.execute('DELETE FROM oauth_flows WHERE expires<?',(time.time(),))
            db.execute('INSERT INTO oauth_flows(id,purpose,token_hash,expires) VALUES(?,?,?,?)',
                       (flow_id,purpose,hashlib.sha256(token.encode()).hexdigest(),time.time()+1800))
        return {'flow_id':flow_id,'pairing_token':token,'purpose':purpose,
                'client_id':client['client_id'],'client_secret':client['client_secret'],'scopes':SCOPES[purpose]}

    def finish_flow(self, document):
        flow_id, proof = document.get('flow_id',''),document.get('pairing_token','')
        with self.store.db() as db:
            row = db.execute('SELECT * FROM oauth_flows WHERE id=?',(flow_id,)).fetchone()
        if not row or row['used'] or row['expires']<time.time() or not secrets.compare_digest(row['token_hash'],hashlib.sha256(proof.encode()).hexdigest()):
            raise ValueError('Anmeldung ist abgelaufen oder bereits verwendet. Bitte erneut starten.')
        purpose = row['purpose']
        tokens = document.get('tokens',{})
        if not isinstance(tokens,dict) or not tokens.get('refresh_token') or not tokens.get('access_token'):
            raise ValueError('Google hat keine dauerhafte Freigabe geliefert. Bitte erneut anmelden und Zugriff erlauben.')
        if any(len(str(tokens.get(key,'')))>16000 for key in ['refresh_token','access_token']):
            raise ValueError('Ungültige Google-Antwort.')
        info = requests.get('https://oauth2.googleapis.com/tokeninfo',params={'access_token':tokens['access_token']},timeout=25)
        if info.status_code!=200:
            raise ValueError('Google konnte das Zugriffstoken nicht bestätigen.')
        info = info.json()
        client = self.store.secrets()['google_client']
        if info.get('aud')!=client['client_id']:
            raise ValueError('Das Zugriffstoken gehört zu einer anderen Anwendung.')
        required = {s for s in SCOPES[purpose] if s.startswith('https://www.googleapis.com/')}
        if not required.issubset(set(info.get('scope','').split())):
            raise ValueError('Es wurden nicht alle benötigten Google-Berechtigungen freigegeben.')
        who = requests.get('https://openidconnect.googleapis.com/v1/userinfo',
                           headers={'Authorization':'Bearer '+tokens['access_token']},timeout=25)
        if who.status_code!=200 or not who.json().get('email'):
            raise ValueError('Das Google-Konto konnte nicht ermittelt werden.')
        email = who.json()['email']
        previous = self.store.get(purpose+'_account')
        if purpose=='photos' and previous and previous!=email:
            raise ValueError('Die Upload-Liste gehört zu '+previous+'. Bitte dieses Google-Fotos-Konto erneut verbinden.')
        with self.locks[purpose]:

            with self.store.db() as db:
                updated=db.execute('UPDATE oauth_flows SET used=1 WHERE id=? AND used=0 AND expires>?',
                                   (flow_id,time.time())).rowcount
                if updated!=1:
                    raise ValueError('Anmeldung bereits verwendet oder abgelaufen.')
            value = {'access_token':tokens['access_token'],'refresh_token':tokens['refresh_token'],
                     'expires_at':time.time()+int(tokens.get('expires_in',3600)),
                     'scope':info.get('scope',''),'email':email,'connected_at':now()}
            self.store.update_secrets(**{purpose+'_oauth':value})
            self.store.set(purpose+'_account',email)
        return {'purpose':purpose,'email':email}

    def connected(self, purpose):
        token = self.store.secrets().get(purpose+'_oauth')
        return bool(token and token.get('refresh_token'))

    def token(self, purpose, force=False):
        with self.locks[purpose]:
            secure = self.store.secrets()
            value = secure.get(purpose+'_oauth')
            client = secure.get('google_client')
            if not value or not client:
                raise GoogleError('Google '+{'drive':'Drive','photos':'Fotos','monitoring':'Verbrauchsanzeige'}[purpose]+' ist noch nicht verbunden.')
            if not force and value.get('expires_at',0)>time.time()+90:
                return value['access_token']
            try:
                response = requests.post('https://oauth2.googleapis.com/token',data={
                    'client_id':client['client_id'],'client_secret':client['client_secret'],
                    'refresh_token':value['refresh_token'],'grant_type':'refresh_token'},timeout=30)
            except requests.RequestException:
                raise GoogleError('Google-Anmeldung momentan nicht erreichbar.') from None
            if response.status_code!=200:
                raise GoogleError('Google-Freigabe abgelaufen oder ungültig. Bitte in der App erneut verbinden.',response.status_code)
            result = response.json()
            value.update(access_token=result['access_token'],expires_at=time.time()+int(result.get('expires_in',3600)))
            if result.get('refresh_token'):
                value['refresh_token']=result['refresh_token']
            self.store.update_secrets(**{purpose+'_oauth':value})
            return value['access_token']

    def request(self, purpose, method, url, retry=True, **kwargs):
        parsed = urlparse(url)
        if parsed.scheme!='https' or not (parsed.hostname or '').endswith('.googleapis.com'):
            raise GoogleError('Ungültige Google-API-Adresse.')
        headers = dict(kwargs.pop('headers',{}))
        timeout=kwargs.pop('timeout',(20,180))
        for attempt in range(4 if retry else 1):
            headers['Authorization']='Bearer '+self.token(purpose,force=attempt>0 and previous_status==401)
            try:
                response=requests.request(method,url,headers=headers,timeout=timeout,
                                          allow_redirects=False,**kwargs)
            except requests.RequestException:
                if retry and attempt<3:
                    time.sleep(2**attempt)
                    previous_status=0
                    continue
                raise GoogleError('Google-Verbindung unterbrochen. Der Upload wird später fortgesetzt.') from None
            if response.ok:
                return response
            previous_status=response.status_code
            if retry and attempt<3 and response.status_code in [401,429,500,502,503,504]:
                time.sleep(2**attempt)
                continue
            code=''
            try:
                error=response.json().get('error',{})
                if isinstance(error,dict):
                    code=error.get('status','')
            except ValueError:
                pass
            raise GoogleError(f'Google-Anfrage fehlgeschlagen: HTTP {response.status_code} {code}'.strip(),response.status_code)
        raise GoogleError('Google-Anfrage momentan nicht möglich.')

    def rclone_config(self):
        value=self.store.secrets()
        client=value.get('google_client',{})
        self.token('drive')
        oauth=self.store.secrets()['drive_oauth']
        token={'access_token':oauth['access_token'],'token_type':'Bearer','refresh_token':oauth['refresh_token'],
               'expiry':dt.datetime.fromtimestamp(oauth['expires_at'],dt.timezone.utc).isoformat().replace('+00:00','Z')}

        content='[drive]\ntype = drive\nscope = drive.file\nclient_id = '+client['client_id']+'\nclient_secret = '+client['client_secret']+'\ntoken = '+json.dumps(token,separators=(',',':'))+'\n'
        from .core import atomic
        path=self.store.root/'runtime'/'rclone.conf'
        atomic(path,content.encode())
        return path
