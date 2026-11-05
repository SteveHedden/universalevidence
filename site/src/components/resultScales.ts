// Executable counterparts of result-interpretation.ttl display policies.
export type Scale={unit:string;factor:number;ratio?:boolean;relative?:boolean};
const linear=(unit:string,factor=1):Scale=>({unit,factor});
export function scaleFor(unit:string):Scale|undefined{
 const ratios=['odds ratio','hazard ratio','risk ratio','relative risk','relative odds ratio','ratio of odds ratios'];
 if(ratios.includes(unit))return {unit,factor:1,ratio:true};
 if(unit==='prevalence ratio')return {unit:'%',factor:100,ratio:true,relative:true};
 if(['probability difference (0–1 scale)','probability difference','proportion difference','proportion'].includes(unit))return linear('percentage points',100);
 if(['mmHg','%','percentage points','SD','z-score','z-score units','cm','mm','z-score/month','z-score units/month','cm/month','mm/month'].includes(unit))return linear(unit);
 if(unit==='centimeters (cm)')return linear('cm');
 return undefined;
}
export function transformValue(value:number,s:Scale,orientation:number):number|null{
 if(!Number.isFinite(value)||![1,-1].includes(orientation)||s.ratio&&value<=0)return null;
 // Internal coordinates have null zero. Reverse a ratio by reciprocal, not negation.
 return s.ratio ? ((orientation===-1?1/value:value)-1)*s.factor : value*s.factor*orientation;
}
const fmt=(v:number)=>Number(v.toFixed(2)).toString();
export function displayMagnitude(lo:number,hi:number,s:Scale):string{
 if(s.ratio&&!s.relative)return `${lo===hi?fmt(lo+1):`${fmt(lo+1)}–${fmt(hi+1)}`} ${s.unit}`;
 const amount=lo===hi?fmt(Math.abs(lo)):`${fmt(lo)} to ${fmt(hi)}`;
 if(lo!==hi)return `${amount} ${s.unit} (estimate range)`;
 return `${amount}${s.unit==='%'?'':' '}${s.unit==='percentage points'&&Math.abs(lo)===1?'percentage point':s.unit} ${lo<0?'lower':lo>0?'higher':'difference'}${s.relative?' (relative)':''}`;
}
export function displayInterval(lo:number,hi:number,s:Scale):string{
 if(s.ratio&&!s.relative)return `95% interval: ${fmt(lo+1)}–${fmt(hi+1)} (${s.unit})`;
 const unit=s.unit,space=unit==='%'?'':' ';
 const text=lo>=0?`${fmt(lo)}–${fmt(hi)}${space}${unit} higher`:hi<=0?`${fmt(Math.abs(hi))}–${fmt(Math.abs(lo))}${space}${unit} lower`:`${fmt(lo)} to +${fmt(hi)} ${unit}`;
 return `95% interval: ${text}${s.relative?' (relative)':''}`;
}
