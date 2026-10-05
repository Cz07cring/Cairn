import {expect, test} from 'vitest';
import {authorizeToolProposal} from './roles';
test('PLAN cannot register or invoke even read-only tools',()=>{
 expect(()=>authorizeToolProposal('PLAN','read_file',new Set(['read_file']))).toThrow('ROLE_TOOL_FORBIDDEN');
});
test('unregistered tools fail before any execution',()=>{
 expect(()=>authorizeToolProposal('EXECUTE','shell',new Set())).toThrow('TOOL_NOT_ALLOWED');
 expect(authorizeToolProposal('EXECUTE','read_file',new Set(['read_file']))).toEqual({tool:'read_file',requiresBrokerAuthorization:true});
 expect(authorizeToolProposal('FINALIZE','run_tests',new Set(['run_tests']))).toEqual({tool:'run_tests',requiresBrokerAuthorization:true});
});
