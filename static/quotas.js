'use strict';
const ApiQuotas = (() => {
 function view(row,now=Date.now()) {
  const measured=Date.parse(row.measured_at),limit=Number(row.limit);
  const dailyExpired=row.period==='day'&&row.reset_at&&Date.parse(row.reset_at)<=now;
  const used=row.used===null||row.used===undefined||dailyExpired?null:Number(row.used);
  const age=Number.isFinite(measured)?Math.max(0,(now-measured)/1000):null;
  return {used,limit,percent:used===null||limit<=0?null:used/limit*100,
   stale:age!==null&&(row.period==='minute'?age>600:age>900),
   label:row.title+(row.period==='day'?' pro Tag':' pro Minute')+(row.per_user?' und Nutzer':' und Projekt'),
   unavailable:row.per_user?'Google liefert keinen einzelnen Nutzerwert.':dailyExpired?'Neuer Google-Kontingenttag. Messwerte werden geladen.':'Google liefert noch keine Messwerte.'};
 }
 return {view};
})();
if(typeof module!=='undefined')module.exports=ApiQuotas;
