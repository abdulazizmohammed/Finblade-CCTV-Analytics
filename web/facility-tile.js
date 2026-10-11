// The "In facility" KPI tile's subtitle — what the door-counted roster can
// honestly say about itself.
//
// Shared by the wall dashboard and the operations console. It lived inside
// dashboard.html until ops.html needed the same tile; a second copy would be
// a second thing to keep true, and this text is where the caveats live.
// tests/test_facility_tile.py runs these under node, so they stay pure: no
// DOM, no fetch, no page globals.

function facilityWindow(s){
  const v=+s||0;
  return v>=3600?`${Math.round(v/3600)}h`:`${Math.round(v/60)}m`;
}

function facilitySub(f){
  const policyDoors=(f&&f.policy&&f.policy.doors)||[];
  // No door zones means the count can never be anything but a declared baseline.
  // A confident "0 people" would read as "the building is empty" when in fact
  // nothing is being counted.
  if(!policyDoors.length) return 'no door zones configured — cannot be counted';
  const doors=(f&&f.doors)||[];
  const ein=doors.reduce((s,d)=>s+(+d.entry_rate_per_min||0),0);
  const eout=doors.reduce((s,d)=>s+(+d.exit_rate_per_min||0),0);
  // Site totals only. Which door is busy is a detail-view question and would
  // crowd out the headline here.
  const parts=[`↑${ein.toFixed(1)} ↓${eout.toFixed(1)} /min`];
  // The decomposition, only when there is a baseline to decompose. At zero it
  // says nothing and costs a line.
  const base=+f.baseline||0;
  if(base>0) parts.push(`${+f.observed||0} observed · ${base} declared`);
  // Drift: roster entries nobody has seen. Either a person in an unmonitored
  // space or a missed exit, and nothing here can tell which — so it is surfaced
  // for a human rather than acted on.
  const stale=+f.stale||0;
  if(stale>0) parts.push(
    `<span class="drift">${stale} unseen &gt;${facilityWindow(f.stale_after_s)}</span>`);
  return parts.join(' · ');
}

// node (the tests) requires these; a browser just uses the globals above.
if(typeof module!=='undefined'&&module.exports){module.exports={facilityWindow,facilitySub};}
