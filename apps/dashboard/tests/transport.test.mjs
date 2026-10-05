import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeBatch,displayFresh} from '../src/transport.mjs';
test('snapshot, ordered patch and fresh connection',()=>{
  const a=mergeBatch(null,{kind:'snapshot',sequence:0,data:{health:'HEALTHY',risk:'BLOCKED'}},-1);
  const b=mergeBatch(a.data,{kind:'patch',sequence:1,data:{health:'STALE'}},a.sequence);
  assert.deepEqual(b.data,{health:'STALE',risk:'BLOCKED'});
  assert.equal(displayFresh(1000,2600),false);
});
test('gap, duplicate and missing baseline require resnapshot',()=>{
  for(const seq of [0,2])assert.throws(()=>mergeBatch({},{kind:'patch',sequence:seq,data:{}},0));
  assert.throws(()=>mergeBatch(null,{kind:'patch',sequence:0,data:{}},-1));
});
