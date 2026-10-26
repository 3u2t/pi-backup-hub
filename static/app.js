'use strict';
const $ = id => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let state = null, populated = false, runningJob = null, toastTimer = null;
const labels = {overview:['Alles im Blick.','Festplatten-Backups und Foto-Uploads, unabhängig voneinander.'],schedule:['Deine HDD. Dein Zeitplan.','Neue und geänderte Dateien als verschlüsselte Sicherungsstände aufbewahren.'],photos:['Neue Erinnerungen sichern.','Neue Immich-Originale automatisch zu deinem Google-Fotos-Konto hochladen.'],accounts:['Deine Google-Verbindungen.','Einmal freigeben. Danach übernimmt dein Raspberry Pi.'],recovery:['Für den Fall der Fälle.','Schlüssel aufbewahren, Sicherungsstände prüfen und Dateien zurückholen.'],activity:['Was der Pi gerade macht.','Aufgaben, Ergebnisse und verständliche Fehlermeldungen.']};
const statusNames = {pending:'Wartet',retry:'Erneuter Versuch',uploading:'Wird übertragen',creating:'Wird importiert',uploaded:'Hochgeladen',baseline:'Bereits importiert',unsupported:'Nicht unterstützt',source_mismatch:'Quelldatei prüfen',needs_review:'Abgleich nötig',success:'Erfolgreich',failed:'Fehlgeschlagen',running:'Läuft',queued:'Wartet',cancelled:'Abgebrochen',interrupted:'Unterbrochen',pausing:'Pause wird vorbereitet',paused:'Pausiert'};
const jobNames = {backup:'HDD sichern',init:'Speicher vorbereiten',check:'Backup prüfen',snapshots:'Sicherungsstände laden',restore_test:'Wiederherstellung testen',drive_test:'Drive testen'};
const escapeHtml = text => String(text ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
const number = value => new Intl.NumberFormat('de-DE').format(value || 0);
function bytes(value) {
 if(value == null) return '—';
 const units=['Byte','Kilobyte','Megabyte','Gigabyte','Terabyte','Petabyte'];
 let n=Number(value),i=0;
 while(n>=1000&&i<units.length-1){n/=1000;i++;}
 return new Intl.NumberFormat('de-DE',{maximumFractionDigits:i?1:0}).format(n)+' '+units[i];
}
function uploadMegabytes(value) { return Math.min(1048.576,Math.round(Number(value)*1.048576*100)/100); }
function storedUploadLimit(value,original) {
 const entered=Number(value);
 return entered===uploadMegabytes(original)?original:entered/1.048576;
}
function readableUnits(value) {
 return String(value??'').replace(/(\d+(?:[.,]\d+)?)\s*(KiB|MiB|GiB|TiB)(\/s)?\b/g,(_,amount,unit,rate)=>
  bytes(Number(amount.replace(',','.'))*1024**(['KiB','MiB','GiB','TiB'].indexOf(unit)+1))+(rate?' pro Sekunde':''));
}
function diskAmount(value) {
 const label=bytes(value),separator=label.lastIndexOf(' ');
 $('disk-used').innerHTML=escapeHtml(label.slice(0,separator))+' <span class="storage-unit">'+escapeHtml(label.slice(separator+1))+'</span>';
}
function date(value, short=false) { if(!value)return 'Noch nicht';return new Intl.DateTimeFormat('de-DE',{timeZone:state?.timezone||'Europe/Berlin',day:'2-digit',month:'2-digit',...(short?{}:{year:'numeric'}),hour:'2-digit',minute:'2-digit'}).format(new Date(value)); }
function toast(message,error=false){$('toast').textContent=readableUnits(message);$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},error?10000:5000);}
async function api(path, body, options={}) { const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(body===undefined?{}:{body:JSON.stringify(body)}),...options});if(response.status===401){location.href='/login';throw new Error('Bitte anmelden.');}const result=await response.json();if(!response.ok)throw new Error(result.error||'Die Anfrage ist fehlgeschlagen.');return result; }
function selectTab(name){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('active',e.id==='tab-'+name));document.querySelectorAll('.nav-item').forEach(e=>e.classList.toggle('active',e.dataset.tab===name));$('page-title').textContent=labels[name][0];$('page-subtitle').textContent=labels[name][1];if(name==='activity')loadLogs();}
document.querySelectorAll('[data-tab]').forEach(e=>e.addEventListener('click',()=>selectTab(e.dataset.tab)));
document.querySelectorAll('[data-goto]').forEach(e=>e.addEventListener('click',()=>selectTab(e.dataset.goto)));
function badge(id,text,good=false,warn=false){$(id).textContent=text;$(id).classList.toggle('good',good);$(id).classList.toggle('warn',warn);}
function scheduleText(s){const d=s.drive;if(!d.enabled)return 'Pausiert';if(d.schedule==='interval')return 'Alle '+d.interval_hours+' Stunden';const days=['Mo','Di','Mi','Do','Fr','Sa','So'];return (d.schedule==='daily'?'Täglich':d.weekdays.map(x=>days[x]).join(', '))+' um '+d.time;}
function toggleScheduleFields(){const kind=$('drive-schedule').value;$('time-field').hidden=kind==='interval';$('interval-field').hidden=kind!=='interval';$('weekdays-field').hidden=kind!=='weekly';}
$('drive-schedule').addEventListener('change',toggleScheduleFields);
function populate(s){const d=s.settings.drive,p=s.settings.photos;$('drive-enabled').checked=d.enabled;$('drive-folder').value=d.folder;$('timezone').value=s.settings.timezone;$('drive-schedule').value=d.schedule;$('drive-time').value=d.time;$('drive-interval').value=d.interval_hours;$('drive-limit').value=uploadMegabytes(d.upload_mib);$('drive-exclude').value=d.exclude.join('\n');$('retention-enabled').checked=d.retention_enabled;$('keep-daily').value=d.keep_daily;$('keep-weekly').value=d.keep_weekly;$('keep-monthly').value=d.keep_monthly;document.querySelectorAll('#weekdays-field input').forEach(e=>e.checked=d.weekdays.includes(Number(e.value)));$('photos-enabled').checked=p.enabled;$('photos-poll').value=p.poll_seconds;$('photos-limit').value=uploadMegabytes(p.upload_mib);$('photos-album').value=p.album_name;$('photos-videos').checked=p.include_videos;toggleScheduleFields();populated=true;}
function render(s){state=s;if(!populated)populate(s);$('live-status').textContent='Pi verbunden';$('footer-time').textContent='Pi-Zeit: '+date(s.server_time);$('global-error').hidden=s.source.ok;if(!s.source.ok)$('global-error').textContent=s.source.error;if(s.source.ok)diskAmount(s.source.used);else $('disk-used').textContent='Nicht verfügbar';$('disk-label').textContent=s.source.ok?'von '+bytes(s.source.total)+' · '+bytes(s.source.free)+' frei':'Bitte USB-Festplatte prüfen';$('disk-progress').value=s.source.ok?s.source.used/s.source.total*100:0;$('last-backup').textContent=s.last_backup_success?date(s.last_backup_success,true):'Noch keine';$('drive-status').textContent=s.connections.drive.connected?s.connections.drive.email:'Google Drive verbinden';const counts=s.photos_counts,pending=(counts.pending||0)+(counts.retry||0)+(counts.uploading||0)+(counts.creating||0);$('photos-pending').textContent=number(pending);$('photos-queue-label').textContent=(counts.retry||0)?number(counts.retry)+' warten auf erneuten Versuch':s.settings.photos.enabled?'Foto-Automatik aktiv':'Foto-Automatik pausiert';$('photos-done').textContent=number(counts.uploaded);$('photos-last').textContent=s.last_photos_upload?'Zuletzt '+date(s.last_photos_upload,true):'Noch kein neuer Upload';badge('drive-badge',!s.connections.drive.connected?'Anmelden':!s.settings.repository_ready?'Vorbereiten':s.settings.drive.enabled?'Zeitplan aktiv':'Bereit',s.settings.repository_ready);badge('photos-badge',!s.connections.photos.connected?'Anmelden':s.settings.photos.enabled?'Automatisch':'Pausiert',s.connections.photos.connected&&s.settings.photos.enabled);$('schedule-summary').textContent=scheduleText(s.settings);$('snapshot-count').textContent=number(s.snapshots.length);$('baseline-count').textContent=number(s.baseline?.count||0)+' Dateien';$('baseline-note').textContent=number(s.baseline?.count||0)+' bereits manuell hochgeladene Dateien';$('last-scan').textContent=s.last_scan?.ok?date(s.last_scan.at,true):s.last_scan?.error||'Wird vorbereitet';$('review-count').textContent=number(s.needs_review);$('source-path').textContent=s.source_path||'Nicht eingerichtet';badge('repository-badge',s.settings.repository_ready?'Bereit':'Noch nicht bereit',s.settings.repository_ready);badge('client-badge',s.client_configured?'Eingerichtet':'JSON-Datei fehlt',s.client_configured);$('client-info').textContent=s.client_configured?'Google-Projekt: '+(s.project_id||'Client-Datei eingerichtet'):'';['drive','photos'].forEach(name=>{$(name+'-account').textContent=s.connections[name].email||'Noch kein Konto verbunden';document.querySelectorAll('.oauth-button[data-purpose="'+name+'"]').forEach(b=>b.disabled=!s.client_configured);document.querySelectorAll('.disconnect[data-purpose="'+name+'"]').forEach(b=>b.hidden=!s.connections[name].connected);});badge('key-badge',s.settings.recovery_key_saved?'Separat gespeichert':'Noch bestätigen',s.settings.recovery_key_saved);$('key-saved').disabled=s.settings.recovery_key_saved;$('photos-enabled').disabled=!s.connections.photos.connected;$('drive-enabled').disabled=!s.connections.drive.connected||!s.settings.repository_ready;const busy=s.jobs.some(j=>['queued','running','pausing','paused'].includes(j.status));document.querySelectorAll('[data-job]').forEach(button=>{const kind=button.dataset.job;button.disabled=busy||!s.connections.drive.connected||(kind==='init'&&!s.settings.recovery_key_saved)||(['backup','check','snapshots','restore_test'].includes(kind)&&!s.settings.repository_ready);});$('reconcile-photos').disabled=!s.connections.photos.connected;runningJob=s.jobs.find(j=>['queued','running','pausing','paused'].includes(j.status));$('running-panel').hidden=!runningJob;if(runningJob)renderBackupProgress(runningJob,s);
 $('media-table').innerHTML=s.media.length?s.media.map(m=>'<tr><td>'+escapeHtml(m.name)+'</td><td><span class="status-text '+escapeHtml(m.status)+'">'+escapeHtml(statusNames[m.status]||m.status)+'</span></td><td>'+escapeHtml(m.error?readableUnits(m.error):((m.uploaded&&m.size)?bytes(m.uploaded)+' / '+bytes(m.size):''))+'</td></tr>').join(''):'<tr><td colspan="3" class="empty">Noch keine neuen Dateien. Der bestehende Bestand ist vorgemerkt.</td></tr>';
 $('jobs-table').innerHTML=s.jobs.length?s.jobs.map(j=>'<tr><td>'+escapeHtml(jobNames[j.kind]||j.kind)+'</td><td><span class="status-text '+escapeHtml(j.status)+'">'+escapeHtml(statusNames[j.status]||j.status)+'</span></td><td>'+escapeHtml(date(j.started||j.created))+'</td><td>'+escapeHtml(readableUnits(j.summary))+'</td></tr>').join(''):'<tr><td colspan="4" class="empty">Noch keine Aufgaben gestartet.</td></tr>';
 $('snapshots-table').innerHTML=s.snapshots.length?[...s.snapshots].reverse().map(x=>'<tr><td>'+escapeHtml(date(x.time))+'</td><td><code>'+escapeHtml(x.short_id||x.id?.slice(0,8))+'</code></td><td>'+escapeHtml(x.hostname)+'</td></tr>').join(''):'<tr><td colspan="3" class="empty">Noch keine Sicherungsstände geladen.</td></tr>';
 $('last-check').textContent=(s.last_repository_check?'Letzte Strukturprüfung: '+date(s.last_repository_check)+'. ':'')+(s.last_restore_test?'Letzter Rückholtest: '+date(s.last_restore_test.at)+'.':'');const progress=s.photos_progress;$('photos-progress-wrap').hidden=!progress||!counts.uploading;if(progress){$('photos-progress-label').textContent=progress.name+' · '+bytes(progress.bytes)+' / '+bytes(progress.total);$('photos-progress').value=progress.total?progress.bytes/progress.total*100:0;}}
function renderApiLimitLinks(s) {
 for(const [purpose,service] of [['photos','photoslibrary.googleapis.com'],['drive','drive.googleapis.com']]) {
  const link=$(purpose+'-quota-link');
  if(!link)continue;
  const url=new URL('https://console.cloud.google.com/apis/api/'+service+'/quotas');
  if(s.project_id)url.searchParams.set('project',s.project_id);
  link.href=url.href;
 }
 const monitoring=s.connections.monitoring||{},q=s.api_quotas||{status:'not_connected'},current=Date.parse(s.server_time);
 $('connect-monitoring').hidden=Boolean(monitoring.connected);
 $('connect-monitoring').disabled=!s.client_configured;
 $('disconnect-monitoring').hidden=!monitoring.connected;
 $('quota-connect-notice').hidden=Boolean(monitoring.connected);
 $('monitoring-account').textContent=monitoring.email?'Lesefreigabe: '+monitoring.email:'';
 $('refresh-quotas').disabled=!monitoring.connected;
 const local=q.mode==='local',stale=!local&&q.fetched_at&&current-Date.parse(q.fetched_at)>180000;
 badge('quota-status',!monitoring.connected?'Lesefreigabe fehlt':q.status==='error'?'Abruffehler':local?'App-Zähler live':stale?'Daten veraltet':q.status==='loading'?'Wird geladen':'Google-Messwerte',(q.status==='ok'||local)&&!stale,q.status==='error'||stale);
 $('quota-error').hidden=!q.error;
 $('quota-error').textContent=q.error||'';
 $('quota-updated').textContent=local?'App-Anfragen erfasst seit '+date(q.local?.started_at)+'. Anzeige aktualisiert: '+date(q.local?.measured_at,true):q.fetched_at?'Zuletzt bei Google abgefragt: '+date(q.fetched_at,true)+(stale?' · Daten veraltet':''):'Noch keine Verbrauchsdaten abgerufen.';
 $('quota-description').textContent=local?'Kostenlose Live-Zählung der Anfragen dieser App. Die zugehörigen Projektlimits werden bei Google gelesen.':'Echte Verbrauchsdaten deines Google-Projekts. Automatische Aktualisierung jede Minute, solange das Dashboard geöffnet ist.';
 $('quota-freshness').textContent=local?'Google verlangt für seinen gesamten Verbrauchszähler eine aktivierte Abrechnung. Hier wird deshalb nur der eigene App-Verbrauch ab Beginn der Erfassung gezählt, nicht der gesamte Google-Projektverbrauch. Frühere Anfragen und andere Apps fehlen. Drive wird alle zwei Sekunden abgelesen; kurze Hilfsaufrufe können fehlen. Googles gewichtete Einheiten sind damit nicht messbar.':'Google meldet Messwerte mit einigen Minuten Verzögerung. Drive zeigt den zuletzt gemeldeten Minutenwert; Google Fotos die Summe des aktuellen Google-Kontingenttags. Tageslimits werden um Mitternacht US-Pazifikzeit zurückgesetzt. Fehlende Messwerte werden als „—“ angezeigt.';
 for(const purpose of ['photos','drive']) {
  const rows=q.services?.[purpose]?.limits||[];
  $(purpose+'-quota-rows').innerHTML=rows.length?rows.map(row=>{
   const model=ApiQuotas.view(row,current),percent=model.percent===null?'—':new Intl.NumberFormat('de-DE',{maximumFractionDigits:2}).format(model.percent)+' %';
   return '<div class="quota-row"><div class="quota-row-heading"><span>'+escapeHtml(model.label)+'</span><b class="'+(model.percent>=90?'quota-high':'')+'">'+escapeHtml(percent)+'</b></div><strong class="quota-amount">'+(model.used===null?'—':number(model.used))+' <small>/ '+(model.limit<0?'Unbegrenzt':number(model.limit))+'</small></strong>'+(model.percent===null?'':'<progress max="100" value="'+Math.min(100,model.percent)+'" aria-label="'+escapeHtml(model.label)+'"></progress>')+'<small class="quota-row-note">'+escapeHtml(model.used===null?model.unavailable:(local?'App-Zähler: ':model.stale?'Letzter gemeldeter Wert: ':'Google-Messwert: ')+date(row.measured_at,true))+'</small></div>';
  }).join(''):'<p>'+escapeHtml(!monitoring.connected?'Wartet auf die Google-Lesefreigabe.':q.status==='error'?'Verbrauch momentan nicht verfügbar.':'Google liefert noch keine Messwerte.')+'</p>';
 }
 for(const [id,service] of [['monitoring-api-link','monitoring.googleapis.com'],['serviceusage-api-link','serviceusage.googleapis.com']]) {
  const url=new URL('https://console.cloud.google.com/apis/library/'+service);
  if(s.project_id)url.searchParams.set('project',s.project_id);
  $(id).href=url.href;
 }
}
function renderBackupProgress(job,s) {
 const model=BackupProgress.view(job,s.network_upload,s.server_time),backup=job.kind==='backup';
 $('running-label').textContent=jobNames[job.kind]||job.kind;
 $('backup-metrics').hidden=!backup;
 $('pause-job').hidden=!backup||model.paused;
 $('pause-job').disabled=job.status!=='running'&&job.status!=='queued';
 $('pause-job').textContent=model.pausing?'Pause wird vorbereitet …':'Pausieren';
 $('resume-job').hidden=!backup||!model.paused;
 $('job-percent').textContent=model.percent===null?'—':new Intl.NumberFormat('de-DE',{maximumFractionDigits:2}).format(model.percent)+' %';
 if(model.percent===null)$('job-progress').removeAttribute('value');else $('job-progress').value=model.percent;
 $('job-phase').textContent=model.paused?'Pausiert':model.pausing?'Wird angehalten':!job.progress?'Wird vorbereitet':model.percent>=99.9?'Wird abgeschlossen':model.scanComplete?'Sicherung läuft':'Dateiliste wird ermittelt';
 $('job-phase').classList.toggle('warn',model.paused||model.pausing);
 $('running-panel').classList.toggle('is-paused',model.paused);
 const p=job.progress;
 $('job-progress-text').textContent=p?bytes(p.bytes_done)+' von '+bytes(p.total_bytes)+' · '+number(p.files_done)+' von '+number(p.total_files)+' Dateien':'Aufgabe wird vorbereitet.';
 $('backup-speed').textContent=model.speed===null?'—':new Intl.NumberFormat('de-DE',{maximumFractionDigits:2}).format(model.speed/1000000);
 $('backup-remaining').textContent=model.paused?'Pausiert':model.pausing?'Wird angehalten':BackupProgress.duration(model.remaining);
 $('backup-finish').textContent=model.finish?date(model.finish,true):model.paused?'Nach dem Fortsetzen':'Wird berechnet';
 $('backup-processed').textContent=model.done===null?'—':bytes(model.done);
 $('backup-note').textContent=!backup?'':model.paused?'Die HDD-Sicherung ist pausiert. Fortsetzen prüft die Dateien erneut und verwendet gespeicherte Daten wieder. Die Foto-Automatik läuft separat weiter.':model.pausing?'Die HDD-Sicherung wird sicher angehalten. Das kann kurz dauern, während bereits übertragene Daten gespeichert werden.':!model.scanComplete?'Die Dateiliste wird noch ermittelt. Der Prozentwert ist vorläufig; eine Restzeit erscheint nach dem Scan.':'Die Restzeit ist eine Schätzung und kann sich ändern. Der Uploadwert misst den Netzwerkverkehr dieser App; die Quelldatenmenge berücksichtigt auch wiederverwendete Daten.';
 if(backup&&!model.paused&&!model.pausing)$('backup-note').textContent+=' Nach dem Fortsetzen zählt dieser Prüflauf neu. Bereits gespeicherte Backupdaten bleiben erhalten.';
}
async function refresh(){try{const s=await api('/api/state');render(s);renderApiLimitLinks(s);}catch(error){$('live-status').textContent='Verbindung unterbrochen';if(!state)toast(error.message,true);}}
async function action(path,body={},message='Gespeichert.'){try{await api(path,body);toast(message);await refresh();}catch(error){toast(error.message,true);}}
document.querySelectorAll('[data-job]').forEach(button=>button.addEventListener('click',()=>action('/api/jobs/'+button.dataset.job,{},'Aufgabe gestartet.')));
$('drive-form').addEventListener('submit',async event=>{event.preventDefault();if($('retention-enabled').checked&&!state.settings.drive.retention_enabled&&!confirm('Die aktivierten Aufbewahrungsregeln dürfen ältere Sicherungsstände nach einem erfolgreichen Backup endgültig entfernen. Aktivieren?'))return;const drive={enabled:$('drive-enabled').checked,folder:$('drive-folder').value,schedule:$('drive-schedule').value,time:$('drive-time').value,weekdays:[...document.querySelectorAll('#weekdays-field input:checked')].map(x=>Number(x.value)),interval_hours:Number($('drive-interval').value),upload_mib:storedUploadLimit($('drive-limit').value,state.settings.drive.upload_mib),exclude:$('drive-exclude').value.split('\n').map(x=>x.trim()).filter(Boolean),retention_enabled:$('retention-enabled').checked,keep_daily:Number($('keep-daily').value),keep_weekly:Number($('keep-weekly').value),keep_monthly:Number($('keep-monthly').value)};await action('/api/settings',{timezone:$('timezone').value,drive},'HDD-Einstellungen gespeichert.');});
$('photos-form').addEventListener('submit',event=>{event.preventDefault();action('/api/settings',{photos:{enabled:$('photos-enabled').checked,poll_seconds:Number($('photos-poll').value),include_videos:$('photos-videos').checked,upload_mib:storedUploadLimit($('photos-limit').value,state.settings.photos.upload_mib),album_name:$('photos-album').value}},'Foto-Automatik gespeichert.');});
$('password-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/password',{current:$('current-password').value,new:$('new-password').value});$('password-form').reset();toast('Passwort geändert.');}catch(error){toast(error.message,true);}});
$('client-file').addEventListener('change',async event=>{try{const file=event.target.files[0];if(!file)return;if(file.size>50000)throw new Error('Diese Datei ist zu groß für eine Google-Client-Datei.');await api('/api/client',JSON.parse(await file.text()));toast('Google-Client eingerichtet. Jetzt die Konten verbinden.');await refresh();}catch(error){toast(error.message,true);}finally{event.target.value='';}});
document.querySelectorAll('.oauth-button').forEach(button=>button.addEventListener('click',async()=>{try{button.disabled=true;const purpose=button.dataset.purpose,response=await fetch('/api/oauth/'+purpose+'/helper',{method:'POST',headers:{'X-CSRF-Token':csrf,'Content-Type':'application/json'},body:'{}',credentials:'same-origin'});if(!response.ok){const error=await response.json();throw new Error(error.error||'Anmeldung konnte nicht vorbereitet werden.');}const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='google-'+purpose+'-anmelden.py';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Anmeldehelfer heruntergeladen. Auf diesem Computer mit Python 3 öffnen.');}catch(error){toast(error.message,true);}finally{button.disabled=false;}}));
document.querySelectorAll('.disconnect').forEach(button=>button.addEventListener('click',()=>{if(confirm('Diese Verbindung trennen und die zugehörige Automatik pausieren? Vorhandene Google-Dateien bleiben erhalten.'))action('/api/oauth/'+button.dataset.purpose+'/disconnect',{},'Verbindung getrennt.');}));
$('key-saved').addEventListener('click',()=>action('/api/recovery-key/saved',{confirmed:true},'Schlüssel als separat gespeichert bestätigt.'));
$('pause-job').addEventListener('click',()=>{if(runningJob)action('/api/jobs/'+runningJob.id+'/pause',{},'Pause wird vorbereitet.');});
$('resume-job').addEventListener('click',()=>{if(runningJob)action('/api/jobs/'+runningJob.id+'/resume',{},'Sicherung wird fortgesetzt. Gespeicherte Daten werden wiederverwendet.');});
$('cancel-job').addEventListener('click',()=>{if(runningJob)action('/api/jobs/'+runningJob.id+'/cancel',{},'Abbruch angefordert.');});
['scan-now','photos-scan'].forEach(id=>$(id).addEventListener('click',()=>action('/api/photos/scan',{},'Immich wird erneut geprüft.')));
$('retry-photos').addEventListener('click',()=>action('/api/photos/retry',{},'Fehlgeschlagene Uploads werden erneut versucht.'));
$('reconcile-photos').addEventListener('click',()=>action('/api/photos/reconcile',{},'Google-Status wird im Hintergrund abgeglichen.'));
async function loadLogs(){try{const result=await api('/api/logs');$('log-output').textContent=result.logs.length?result.logs.map(x=>date(x.created)+'  ['+x.level+']  '+readableUnits(x.message)).join('\n'):'Noch keine Einträge.';}catch(error){toast(error.message,true);}}
$('refresh-logs').addEventListener('click',loadLogs);$('refresh-state').addEventListener('click',refresh);$('logout').addEventListener('click',async()=>{await api('/api/logout',{});location.href='/login';});
$('refresh-quotas').addEventListener('click',()=>action('/api/quotas/refresh',{},'Aktualisierung vorgemerkt. Google-Abfragen erfolgen höchstens einmal pro Minute.'));
refresh();setInterval(refresh,5000);
