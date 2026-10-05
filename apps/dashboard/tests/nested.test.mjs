import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeBatch} from '../src/transport.mjs';
test('quote patch retains historical candles and null suppresses old metrics',()=>{
 const p={market:{candles:[{time:1}],quote:{bid:'100'},depth:{metrics:{spread:'2'}}}};
 const b=mergeBatch(p,{kind:'patch',sequence:1,data:{market:{quote:{bid:'101'},depth:{metrics:null}}}},0);
 assert.deepEqual(b.data.market.candles,p.market.candles);
 assert.equal(b.data.market.quote.bid,'101');assert.equal(b.data.market.depth.metrics,null);
});

test('bounded candle/footprint patches append, revise and evict by identity',()=>{
 const p={flow:{candles:[{time:1,delta:'2'},{time:2,delta:'3'}]}};
 const batch={kind:'patch',sequence:1,data:{flow:{candles:{$merge_by:'time',$retain:[2,3],$rows:[{time:2,delta:'4'},{time:3,delta:'5'}]}}}};
 const result=mergeBatch(p,batch,0);
 assert.deepEqual(result.data.flow.candles,[{time:2,delta:'4'},{time:3,delta:'5'}]);
});
