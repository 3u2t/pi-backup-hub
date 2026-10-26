'use strict';
const BackupProgress = (() => {
 function numeric(value) {
  return typeof value==='number'&&Number.isFinite(value)?value:null;
 }
 function duration(seconds) {
  if(numeric(seconds)===null||seconds<0)return 'Wird berechnet';
  const minutes=Math.max(1,Math.ceil(seconds/60));
  const days=Math.floor(minutes/1440),hours=Math.floor(minutes%1440/60),rest=minutes%60;
  if(days)return days+' '+(days===1?'Tag':'Tage')+(hours?' '+hours+' Std.':'');
  if(hours)return hours+' Std.'+(rest?' '+rest+' Min.':'');
  return minutes+' Min.';
 }
 function view(job,network,serverTime) {
  const progress=job?.progress||{};
  const total=numeric(progress.total_bytes),done=numeric(progress.bytes_done);
  const fraction=numeric(progress.percent_done)??(total>0&&done!==null?done/total:null);
  const percent=fraction===null?null:Math.max(0,Math.min(99.9,fraction*100));
  const paused=job?.status==='paused',pausing=job?.status==='pausing';
  const age=(Date.parse(serverTime)-Date.parse(progress.updated_at))/1000;
  const seconds=numeric(progress.seconds_remaining);
  const eta=job?.status==='running'&&progress.scan_complete&&progress.seconds_elapsed>=30&&age>=0&&age<=15&&seconds>0?seconds:null;
  const measuredAge=(Date.parse(serverTime)-Date.parse(network?.measured_at))/1000;
  const measured=numeric(network?.bytes_per_second);
  const speed=measuredAge>=0&&measuredAge<=8&&measured!==null?Math.max(0,measured):null;
  return {percent,done,total,paused,pausing,speed,remaining:eta,finish:eta===null?null:new Date(Date.parse(serverTime)+eta*1000).toISOString(),scanComplete:!!progress.scan_complete};
 }
 return {duration,view};
})();
if(typeof module!=='undefined')module.exports=BackupProgress;
