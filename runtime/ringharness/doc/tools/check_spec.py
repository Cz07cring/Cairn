"""Check frozen documentation and fixed Content vectors, not application behavior."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from zipfile import ZipFile
from content_codec import SCHEMA, encode
from check_readiness import check as check_readiness
from check_gate import check as check_gate

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / 'releases/v0.5'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check(allow_pending_freeze=False):
    errors = []
    files = sorted(ROOT.glob('*.md'))
    links = examples = 0
    for p in files:
        text = p.read_text()
        if 'v0.5' not in text:
            errors.append(p.name + ': wrong version')
        if sum(line.startswith('```') for line in text.splitlines()) % 2:
            errors.append(p.name + ': fence mismatch')
        columns = None
        for line in text.splitlines():
            if line.startswith('|'):
                count = len(re.findall(r'(?<!\\)\|', line))
                if columns is not None and count != columns:
                    errors.append(p.name + ': table column mismatch')
                columns = count
            else:
                columns = None
        for link in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text):
            if '://' in link:
                continue
            path, _, anchor = link.partition('#')
            target = p.parent / path if path else p
            pending = allow_pending_freeze and target.parent == RELEASE and target.name in {'manifest.json','snapshot.zip','check-report.json'}
            if not target.exists() and not pending:
                errors.append(p.name + ': broken link ' + link)
            elif anchor and target.exists():
                heads = [re.sub(r'[^\w\-\u4e00-\u9fff ]', '', h.lstrip('#').strip()).lower().replace(' ', '-') for h in target.read_text().splitlines() if h.startswith('#')]
                if anchor not in heads:
                    errors.append(p.name + ': broken anchor ' + link)
            links += 1
        for block in re.findall(r'```json\n(.*?)\n```', text, re.S):
            try: json.loads(block); examples += 1
            except ValueError: errors.append(p.name + ': invalid JSON')
    if len(files) != 11:
        errors.append('expected 10 numbered documents plus README')
    for i in range(1,11):
        if len(list(ROOT.glob(f'{i:02}-*.md'))) != 1:
            errors.append('numbered document missing')
    api = (ROOT / '05-API接口文档.md').read_text()
    dev = (ROOT / '01-开发文档.md').read_text()
    test = (ROOT / '02-测试文档.md').read_text()
    audit = (ROOT / '10-架构审计与改进建议.md').read_text()
    e2e = (ROOT / '06-E2E开发调试文档.md').read_text()
    kinds = re.search(r'ActivityKind=`([^`]+)`', api).group(1).split(',')
    dk = re.search(r'ActivityKind 固定为 `([^`]+)`', dev).group(1).replace(' ', '').split(',')
    if kinds != dk or len(kinds) != 10:
        errors.append('ActivityKind mismatch')
    for k in kinds:
        if f'{k}={{' not in api or f'| {k} |' not in api:
            errors.append('missing outcome/role mapping ' + k)
    for prefix, count in [('T',24),('AT',8),('E',10)]:
        target = e2e if prefix == 'E' else test
        for i in range(1,count+1):
            token = f'| {prefix}{i:02} ' if prefix == 'AT' else f'| {prefix}{i:02} |'
            if token not in target:
                errors.append('missing ' + token)
    for prefix, count in [('A',8),('B',8),('C',4),('D',4),('DEV',6),('RD',6),('FG',2)]:
        for i in range(1,count+1):
            key=f'{prefix}{i:02}'
            if f'| {key} ' not in audit:
                errors.append('missing closure ' + key)
    for token in ['MODEL_INVOCATION','ModelInvocationResource','FinalizationRecoveryRequest','RECOVER_FINALIZATION','StopRequest','StopResource','StopReceipt','ExecutionBinding','BindingAdoption','global_audits:','planning_feedback_ids:','ROLE_TOOL_FORBIDDEN','VerificationAssessment','VerificationObligation','TrustPropagationJob','VALIDATION_EVIDENCE_INVALID','contracts/canonicalization-v3.json']:
        if token not in api: errors.append('missing protocol ' + token)
    for stale in ['consumed_effect_id','stop(ActivationRef,reason)→StopReceipt','未预授权L4先生成绑定effect']:
        if any(stale in p.read_text() for p in files): errors.append('stale protocol ' + stale)
    routes=[];section=''
    for line in api.splitlines():
        if line.startswith('## 5.'): section='/api/v1'
        elif line.startswith('## 6.'): section='/internal/v1'
        elif line.startswith('## 7.'): section=''
        match=re.match(r'\| (GET|POST|PUT|DELETE|PATCH) (/[^ |]+)',line)
        if match and section: routes.append((match[1],section+match[2]))
    if len(routes) != len(set(routes)): errors.append('duplicate routes')
    def schema_refs(v):
        if isinstance(v,dict):
            if '$ref' in v and v['$ref'].split('/')[-1] not in SCHEMA['$defs']:
                errors.append('unresolved schema ref')
            for child in v.values(): schema_refs(child)
        elif isinstance(v,list):
            for child in v: schema_refs(child)
    schema_refs(SCHEMA)
    rules=json.loads((ROOT/'contracts/canonicalization-v3.json').read_text())
    roots=[x['properties']['object_type']['const'] for x in SCHEMA['oneOf']]
    if roots != rules['content_types'] or len(roots)!=29: errors.append('content schema registry mismatch')
    vectors=json.loads((ROOT/'contracts/hash-vectors-v3.json').read_text())
    for vector in vectors['valid']:
        try:
            encoded=encode(vector['input_json'])
            if encoded.decode()!=vector['canonical_utf8'] or digest(encoded)!=vector['sha256']:
                errors.append('Python vector mismatch '+vector['name'])
        except (ValueError,UnicodeError) as exc: errors.append('Python vector failure '+vector['name']+': '+str(exc))
    for vector in vectors['invalid']:
        try: encode(vector['input_json'])
        except (ValueError,UnicodeError): continue
        errors.append('invalid vector accepted '+vector['name'])
    try:
        result=subprocess.run(['node',str(ROOT/'tools/check_content.mjs')],capture_output=True,text=True,check=True)
        javascript=json.loads(result.stdout)
    except (OSError,ValueError,subprocess.CalledProcessError) as exc:
        javascript={'status':'FAIL'};errors.append('JS vector check failed '+str(exc))
    readiness=check_readiness()
    archives=0
    for zpath in (ROOT/'archive').glob('*.zip'):
        with ZipFile(zpath) as archive:
            if archive.testzip(): errors.append('archive CRC '+zpath.name)
        archives+=1
    return {'spec_version':'v0.5','scope':'documentation and Content fixtures only','markdown_files':len(files),'local_links':links,'json_examples':examples,'activity_kinds':len(kinds),'routes':len(routes),'content_schemas':len(roots),'positive_vectors':len(vectors['valid']),'negative_vectors':len(vectors['invalid']),'javascript':javascript,'readiness':readiness,'gate':check_gate(),'archive_crc_checked':archives,'application_tests':'NOT_RUN','errors':errors,'status':'PASS' if not errors else 'FAIL'}


def verify_freeze():
    manifest_bytes=(RELEASE/'manifest.json').read_bytes()
    manifest=json.loads(manifest_bytes)
    receipt=json.loads((RELEASE/'freeze-receipt.json').read_text())
    archive_bytes=(RELEASE/'snapshot.zip').read_bytes()
    assert digest(manifest_bytes)==receipt['manifest_sha256'],'manifest hash mismatch'
    assert digest(archive_bytes)==receipt['snapshot_sha256'],'snapshot hash mismatch'
    assert manifest['spec_version']=='v0.5' and manifest['status']=='FROZEN_SPEC'
    listed=[item['path'] for item in manifest['files']]
    assert len(listed)==len(set(listed)), 'duplicate manifest entries'
    normative={str(p.relative_to(ROOT)) for pattern in ('*.md','contracts/*.json','tools/*.py','tools/*.mjs') for p in ROOT.glob(pattern)}
    assert normative.issubset(set(listed)), 'unlisted normative file'
    for item in manifest['files']:
        path=ROOT/item['path']
        assert path.resolve().is_relative_to(ROOT.resolve()),'unsafe manifest path'
        data=path.read_bytes()
        assert len(data)==item['bytes'] and digest(data)==item['sha256'],'file changed: '+item['path']
    with ZipFile(RELEASE/'snapshot.zip') as archive:
        assert archive.testzip() is None,'snapshot CRC mismatch'
        expected=set(listed)|{'releases/v0.5/manifest.json'}
        assert len(archive.namelist())==len(expected) and set(archive.namelist())==expected,'snapshot inventory mismatch'
        for item in manifest['files']:
            assert archive.read(item['path'])==(ROOT/item['path']).read_bytes(),'snapshot differs '+item['path']
        assert archive.read('releases/v0.5/manifest.json')==manifest_bytes
    return {'status':'PASS','files':len(manifest['files']),'snapshot_sha256':receipt['snapshot_sha256']}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--allow-pending-freeze',action='store_true')
    parser.add_argument('--verify-freeze',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    report=check(args.allow_pending_freeze)
    if args.verify_freeze:
        try: report['freeze']=verify_freeze()
        except (AssertionError,ValueError,OSError,KeyError) as exc:
            report['errors'].append(str(exc));report['status']='FAIL'
    encoded=json.dumps(report,ensure_ascii=False,indent=2)+'\n'
    if args.report: args.report.write_text(encoded)
    print(encoded,end='')
    raise SystemExit(0 if report['status']=='PASS' else 1)
