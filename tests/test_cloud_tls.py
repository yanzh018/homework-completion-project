"""TLS verification and diagnostic regression checks without sending private data."""
import os
import http.client
import json
import socket
import ssl
import sys
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.error import URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cloud


def main():
    # macOS Python.org builds can have an empty OpenSSL default trust store.
    empty=Mock();empty.cert_store_stats.return_value={'x509_ca':0}
    with patch.object(cloud.ssl,'create_default_context',return_value=empty), \
         patch.dict(os.environ,{},clear=True),patch.object(cloud.os.path,'isfile',return_value=True):
        assert cloud.tls_context() is empty
        empty.load_verify_locations.assert_called_once_with(cafile='/etc/ssl/cert.pem')
    # Never override an explicitly configured CA trust source.
    explicit=Mock();explicit.cert_store_stats.return_value={'x509_ca':0}
    with patch.object(cloud.ssl,'create_default_context',return_value=explicit), \
         patch.dict(os.environ,{'SSL_CERT_FILE':'/custom/ca.pem'},clear=True), \
         patch.object(cloud.os.path,'isfile',return_value=True):
        cloud.tls_context();explicit.load_verify_locations.assert_not_called()
    # A healthy native store remains untouched.
    native=Mock();native.cert_store_stats.return_value={'x509_ca':4}
    with patch.object(cloud.ssl,'create_default_context',return_value=native),patch.dict(os.environ,{},clear=True):
        cloud.tls_context();native.load_verify_locations.assert_not_called()
    context=cloud.tls_context()
    assert context.verify_mode==ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()['x509_ca']>0,'No usable certificate authorities on this test host'
    cfg={'enabled':True,'api_key':'secret-not-for-output','model':'test','base_url':'https://example.invalid',
         'max_tokens':100,'supports_images':True}
    failures=[(ssl.SSLCertVerificationError(1,'untrusted'),'证书校验失败'),
              (socket.gaierror('bad DNS'),'无法解析'),(TimeoutError('timeout'),'超时')]
    for reason,expected in failures:
        with patch.object(cloud,'config',return_value=cfg),patch.object(cloud,'urlopen',side_effect=URLError(reason)):
            try:cloud.chat('test','hello','test',True)
            except ValueError as error:
                assert expected in str(error) and cfg['api_key'] not in str(error),str(error)
            else:raise AssertionError('Expected connection error')
    response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    response.read.return_value=json.dumps({'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}).encode()
    with patch.object(cloud,'config',return_value=cfg),patch.object(cloud,'execute'), \
         patch.object(cloud.time,'sleep'),patch.object(cloud,'urlopen',side_effect=[http.client.RemoteDisconnected('test'),response]) as request:
        text,_=cloud.chat('test','hello','test',True)
        assert text=='ok' and request.call_count==2
    deepseek={**cfg,'base_url':'https://api.deepseek.com'}
    with patch.object(cloud,'config',return_value=deepseek),patch.object(cloud,'execute'), \
         patch.object(cloud,'urlopen',return_value=response) as request:
        cloud.chat('verify_solution','JSON example','test',True)
        payload=json.loads(request.call_args.args[0].data)
        assert payload['response_format']=={'type':'json_object'}
        cloud.chat('verify_glyphs','JSON example','test',True)
        payload=json.loads(request.call_args.args[0].data)
        assert payload['response_format']=={'type':'json_object'} and payload['thinking']=={'type':'disabled'}
        cloud.chat('verify_inferred_glyphs','JSON example','test',True)
        payload=json.loads(request.call_args.args[0].data)
        assert payload['response_format']=={'type':'json_object'} and payload['thinking']=={'type':'disabled'}
    with patch.object(cloud,'config',return_value=cfg),patch.object(cloud.time,'sleep'), \
         patch.object(cloud,'urlopen',side_effect=http.client.RemoteDisconnected('test')) as request:
        try:cloud.chat('test','hello','test',True)
        except ValueError as error:assert '中断' in str(error) and request.call_count==2
        else:raise AssertionError('Expected a clear interruption error')
    print('PASS CA fallback, explicit trust settings, strict TLS, and distinct certificate / DNS / timeout errors')

if __name__=='__main__':main()
