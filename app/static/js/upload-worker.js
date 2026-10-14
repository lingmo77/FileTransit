importScripts('/static/js/upload-hash.js?v=1');
self.onmessage = async function (event) {
  try {
    var file=event.data.file, hash=new FTUploadHash(), step=4*1024*1024;
    for(var pos=0;pos<file.size;pos+=step) {
      hash.update(new Uint8Array(await file.slice(pos,pos+step).arrayBuffer()));
      self.postMessage({progress:Math.min(1,(pos+step)/file.size)});
    }
    self.postMessage({sha256:hash.hex()});
  } catch(error) { self.postMessage({error:'无法读取文件，请重新选择'}); }
};
