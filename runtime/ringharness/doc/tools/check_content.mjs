// Independent JavaScript encoding of valid fixed vectors, not an API validator.
import fs from 'node:fs';
import crypto from 'node:crypto';
const base = new URL('../contracts/', import.meta.url);
const schema = JSON.parse(fs.readFileSync(new URL('content-v3.schema.json', base), 'utf8'));
const vectors = JSON.parse(fs.readFileSync(new URL('hash-vectors-v3.json', base), 'utf8'));
const resolve = s => s.$ref ? schema.$defs[s.$ref.split('/').at(-1)] : s;
function choose(v, s) {
  s = resolve(s);
  if (s.oneOf) return choose(v, s.oneOf.find(x => x.properties.object_type.const === v.object_type));
  if (s.anyOf) {
    const t = v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v === 'number' ? 'integer' : typeof v;
    return choose(v, s.anyOf.find(x => resolve(x).type === t));
  }
  return s;
}
function canon(v) {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v !== null && typeof v === 'object') return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
  if (typeof v === 'number' && (!Number.isSafeInteger(v) || Object.is(v, -0))) throw Error('invalid number');
  return JSON.stringify(v);
}
function norm(v, s) {
  s = choose(v, s);
  if (s.type === 'object') return Object.fromEntries(Object.entries(v).map(([k,x]) => [k,norm(x,s.properties?.[k] ?? s.additionalProperties)]));
  if (s.type === 'array') {
    const items = v.map(x => norm(x,s.items));
    if (s['x-order'] === 'set') {
      const key = x => s['x-sort-keys'] ? s['x-sort-keys'].map(k => String(x[k])) : [canon(x)];
      const compare = (a,b) => {
        const ka=key(a),kb=key(b);
        for(let i=0;i<ka.length;i++) {
          const d=s['x-sort-keys'] ? (ka[i]<kb[i]?-1:ka[i]>kb[i]?1:0) : Buffer.compare(Buffer.from(ka[i]),Buffer.from(kb[i]));
          if(d) return d;
        }
        return 0;
      };
      items.sort(compare);
      for(let i=1;i<items.length;i++) if(compare(items[i-1],items[i])===0) throw Error('duplicate set');
    }
    return items;
  }
  return v;
}
let count=0;
for(const test of vectors.valid) {
  const encoded=canon(norm(JSON.parse(test.input_json),schema));
  const hash=crypto.createHash('sha256').update(encoded,'utf8').digest('hex');
  if(encoded!==test.canonical_utf8 || hash!==test.sha256) throw Error(test.name+' mismatch');
  count++;
}


// Schema boundary fixtures: independent subset validation, no claim of full JSON Schema support.
function valid(v, schemaPart) {
  const s=resolve(schemaPart);
  if(s.allOf && !s.allOf.every(x=>valid(v,x))) return false;
  if(s.if && !valid(v,valid(v,s.if)?(s.then??{}):(s.else??{}))) return false;
  if('const' in s && JSON.stringify(v)!==JSON.stringify(s.const)) return false;
  if(s.enum && !s.enum.includes(v)) return false;
  if(s.oneOf && s.oneOf.filter(x=>valid(v,x)).length!==1) return false;
  if(s.anyOf && !s.anyOf.some(x=>valid(v,x))) return false;
  if(s.type==='null' && v!==null) return false;
  if(s.type==='boolean' && typeof v!=='boolean') return false;
  if(s.type==='integer' && (!Number.isSafeInteger(v)||v<(s.minimum??Number.MIN_SAFE_INTEGER)||v>(s.maximum??Number.MAX_SAFE_INTEGER))) return false;
  if(s.type==='string' && (typeof v!=='string'||[...v].length<(s.minLength??0)||[...v].length>(s.maxLength??Infinity)||(s.pattern&&!new RegExp(s.pattern,'u').test(v)))) return false;
  if(s.type==='array') {
    if(!Array.isArray(v)||v.length<(s.minItems??0)||v.length>(s.maxItems??Infinity)) return false;
    if(!v.every(x=>valid(x,s.items??{}))) return false;
    if(s.contains && v.filter(x=>valid(x,s.contains)).length<(s.minContains??1)) return false;
  }
  if(s.type==='object' || (s.properties && v!==null && typeof v==='object' && !Array.isArray(v))) {
    if(v===null||typeof v!=='object'||Array.isArray(v)) return false;
    if((s.required??[]).some(k=>!(k in v))) return false;
    for(const [k,x] of Object.entries(v)) {
      if(s.properties?.[k]) {if(!valid(x,s.properties[k])) return false;}
      else if(s.additionalProperties===false) return false;
      else if(typeof s.additionalProperties==='object'&&!valid(x,s.additionalProperties)) return false;
    }
  }
  return true;
}
const cases=JSON.parse(fs.readFileSync(new URL('readiness-fixtures-v2.json',base),'utf8')).cases;
for(const c of cases) {
  let part=schema.$defs[c.definition];
  for(const k of c.field_path) part=part.properties[k];
  if(valid(c.value,part)!==c.valid) throw Error('boundary mismatch '+c.name);
}

console.log(JSON.stringify({javascript_valid_vectors:count,field_event_cases:cases.length,status:'PASS'}));
