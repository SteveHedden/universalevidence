import {expect,test} from 'vitest';
import {studyDates,studyStatus} from './organizationMetadata';
test('preserves date precision and distinguishes planned dates',()=>{
 expect(studyDates([{label:'startDateStruct',value:'{"date":"2014-08","type":"ACTUAL"}'},{label:'completionDateStruct',value:'{"date":"2027-06-30","type":"ESTIMATED"}'}])).toEqual([{label:'Started',value:'August 2014',primary:true},{label:'Planned end',value:'30 June 2027',primary:true}]);
});
test('hides administrative fields, malformed JSON and invalid dates',()=>{
 expect(studyDates([{label:'expandedAccessInfo',value:'{"hasExpandedAccess":false}'},{label:'studyFirstSubmitQcDate',value:'2015-04-17'},{label:'completionDateStruct',value:'{bad'},{label:'source_date',value:'2026-02-31'}])).toEqual([]);
 expect(studyStatus('Retained registry status: ACTIVE_NOT_RECRUITING; present operations unverified')).toBe('Active not recruiting');
});
