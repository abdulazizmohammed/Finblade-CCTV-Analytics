// Every time on screen is the SITE's time, not the viewer's.
//
// The pages used toLocaleTimeString() with no timezone, which renders in the
// BROWSER's zone. An operator in Riyadh and a manager reviewing from Dubai or
// London saw different clock times against the same incident, with nothing on
// screen saying which — and a date picker turned "2026-10-11" into the
// viewer's midnight, so a report run from abroad covered the wrong window and
// looked merely odd rather than wrong.
//
// The site zone comes from the API (FINBLADE_BUSINESS_TZ, Asia/Riyadh by
// default); pages that load /api/v1/org/index call setSiteTz with it, and the
// default below is correct for Wareed until one does.
//
// Named zone, never a fixed +03:00: KSA has no daylight saving so the two agree
// there, but a site that does observe it must not be served arithmetic.

let SITE_TZ = 'Asia/Riyadh';
let _explicit = false;          // a page told us; stop guessing

function setSiteTz(tz){ if(tz){ SITE_TZ = String(tz); _explicit = true; } return SITE_TZ; }
function siteTz(){ return SITE_TZ; }

// Pages that do not load the org tree (history, report) still need the right
// zone, so fetch it once. apikey.js has already wrapped fetch with the key.
// Anything a page renders after its own data arrives is therefore correct; the
// only value read before this resolves is a date input's default, which is why
// fbWhenTzReady exists — use it for those.
let _tzReady = null;
function fbWhenTzReady(cb){
  if(_explicit){ cb(); return; }
  if(!_tzReady){
    _tzReady = (typeof fetch==='function'
      ? fetch('/api/v1/org/index').then(r=>r.ok?r.json():null).catch(()=>null)
      : Promise.resolve(null)
    ).then(o=>{ if(o&&o.timezone&&!_explicit) SITE_TZ=String(o.timezone); });
  }
  _tzReady.then(cb);
}

const _fmt = (opts) => new Intl.DateTimeFormat('en-GB', Object.assign({timeZone: SITE_TZ}, opts));

// A falsy timestamp is MISSING, not 1970 — the pages this replaces all wrote
// `ts ? format(ts) : '—'`, and printing an epoch date into an incident log as
// though it were a reading is worse than saying nothing.
// HH:MM:SS at the site.
function fbTime(ts){
  if(!ts) return '—';
  return _fmt({hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(ts*1000));
}
// HH:MM at the site — for axis labels and anywhere seconds are noise.
function fbHM(ts){
  if(!ts) return '—';
  return _fmt({hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(ts*1000));
}
// YYYY-MM-DD at the site. 'en-CA' because it formats exactly that way.
function fbDate(ts){
  const d = ts==null ? new Date() : new Date(ts*1000);
  return new Intl.DateTimeFormat('en-CA',{timeZone:SITE_TZ,year:'numeric',month:'2-digit',day:'2-digit'}).format(d);
}
// YYYY-MM-DD HH:MM:SS at the site, the form the history table wants.
function fbDateTime(ts, seconds=true){
  if(!ts) return '—';
  return `${fbDate(ts)} ${seconds?fbTime(ts):fbHM(ts)}`;
}
// Today's date AT THE SITE — not the viewer's today, which is a different day
// for several hours either side of midnight.
function fbToday(){ return fbDate(null); }

// "GMT+3" — shown beside a clock so nobody has to assume whose time it is.
function fbTzLabel(){
  try{
    const parts = new Intl.DateTimeFormat('en-GB',{timeZone:SITE_TZ,timeZoneName:'shortOffset'})
      .formatToParts(new Date());
    return (parts.find(p=>p.type==='timeZoneName')||{}).value || SITE_TZ;
  }catch(e){ return SITE_TZ; }
}

// How far the site is from UTC at a given instant, in milliseconds. Derived by
// rendering the same instant in both zones rather than hard-coded, so a zone
// with daylight saving gets the offset in force on THAT date.
function _tzOffsetMs(atMs){
  const d = new Date(atMs);
  const asUtc = new Date(d.toLocaleString('en-US',{timeZone:'UTC'}));
  const asSite = new Date(d.toLocaleString('en-US',{timeZone:SITE_TZ}));
  return asSite - asUtc;
}

// Epoch seconds for the START of a site-local date (YYYY-MM-DD).
//
// Two passes: the first uses the offset at UTC midnight, the second re-reads it
// at the candidate instant. Measured against the tz database, one pass is an
// hour out in zones whose clocks move between those two instants (Beirut and
// Chatham in spring, Lord Howe by half an hour); two passes fix all of those.
//
// Then one correction for the case two passes cannot reach: a zone whose clocks
// go FORWARD at midnight (Santiago, Havana) has no 00:00 on that date at all,
// and the arithmetic lands just before the day starts — which would pull an
// hour of the PREVIOUS day into the query. Step forward to the first instant
// that really is on the requested date, as the tz database does.
function fbDayStart(dateStr){
  const utcMidnight = Date.parse(`${dateStr}T00:00:00Z`);
  if(Number.isNaN(utcMidnight)) return null;
  let t = utcMidnight - _tzOffsetMs(utcMidnight);
  t = utcMidnight - _tzOffsetMs(t);
  for(let i=0; i<8 && _dateIn(t) < dateStr; i++) t += 15*60*1000;
  return t/1000;
}
// The site-local date of an instant given in milliseconds.
function _dateIn(ms){
  return new Intl.DateTimeFormat('en-CA',{timeZone:SITE_TZ,year:'numeric',
    month:'2-digit',day:'2-digit'}).format(new Date(ms));
}
// Epoch seconds for the very end of a site-local date (inclusive).
function fbDayEnd(dateStr){
  const start = fbDayStart(dateStr);
  return start==null ? null : start + 86400 - 0.001;
}

if(typeof module!=='undefined'&&module.exports){
  module.exports={setSiteTz,siteTz,fbTime,fbHM,fbDate,fbDateTime,fbToday,fbTzLabel,
                  fbDayStart,fbDayEnd,fbWhenTzReady};
}
