type DateRow = {label:string; value:string};
const labels: Record<string,[string,boolean]> = {
  startDateStruct:['Started',true], completionDateStruct:['Completed',true],
  primaryCompletionDateStruct:['Primary follow-up completed',false],
  Start:['Started',true], End:['Ended',true],
  start_date:['Started',true], end_date:['Ended',true],
  overallEndDate:['Ended',true], recruitmentStart:['Recruitment started',false], recruitmentEnd:['Recruitment ended',false],
  overallTrialStartDate:['Started',true], overallTrialEndDate:['Ended',true],
  recruitmentStartDate:['Recruitment started',false], recruitmentEndDate:['Recruitment ended',false],
  'Start date':['Started',true], 'End date':['Ended',true],
  statusVerifiedDate:['Registry status checked',false],
  studyFirstPostDateStruct:['Registry first published',false],
  lastUpdatePostDateStruct:['Registry updated',false],
  reviewed_at:['Reviewed',false], source_date:['Source updated',false],
};
function readableDate(value:string):string|null {
  const match=/^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:T.*)?$/.exec(value);
  if(!match)return null;
  const [,year,month,day]=match;
  if(!month)return year;
  const m=Number(month);if(m<1||m>12)return null;
  const months=['January','February','March','April','May','June','July','August','September','October','November','December'];
  if(!day)return `${months[m-1]} ${year}`;
  const d=Number(day);const date=new Date(Date.UTC(Number(year),m-1,d));
  if(date.getUTCMonth()!==m-1||d<1)return null;
  return `${d} ${months[m-1]} ${year}`;
}
export function studyDates(rows:DateRow[]) {
  const seen=new Set<string>();
  const expanded=rows.flatMap(row=>{
    if(row.label!=='registry_date_lines')return [row];
    try { const pairs=JSON.parse(row.value);return Array.isArray(pairs)?pairs.filter(v=>Array.isArray(v)&&v.length===2&&['Start date','End date'].includes(v[0])&&typeof v[1]==='string').map(([label,value])=>({label,value})):[];}catch{return [];}
  });
  return expanded.flatMap(row=>{
    const mapping=labels[row.label];if(!mapping)return [];
    let raw=row.value;let estimated=false;
    if(raw.startsWith('{')){try{const value=JSON.parse(raw);raw=value.date;estimated=value.type==='ESTIMATED';}catch{return [];}}
    if(typeof raw!=='string')return [];
    const value=readableDate(raw);if(!value)return [];
    let [label,primary]=mapping;
    if(estimated)label=label==='Started'?'Planned start':label==='Completed'||label==='Ended'?'Planned end':label+' (estimated)';
    const key=label+value;if(seen.has(key))return [];seen.add(key);
    return [{label,value,primary}];
  });
}
export function studyStatus(value:string) {
  const raw=value.replace(/^Retained registry status:\s*/i,'').split(';')[0].trim();
  return raw.replaceAll('_',' ').toLowerCase().replace(/^./,c=>c.toUpperCase());
}
