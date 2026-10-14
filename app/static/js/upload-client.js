/* Sequential, checksummed chunks; status reconciliation makes lost responses safe. */
(function () {
  'use strict';
  var running=0, waiting=[];
  function pump() {
    while(running<2 && waiting.length) {
      running++;
      var job=waiting.shift();
      job.control.cancelQueued=null;
      Promise.resolve().then(job.run).then(job.resolve,job.reject).finally(function(){running--;pump();});
    }
  }
  function slot(run,control) {
    return new Promise(function(resolve,reject){
      var job={run:run,resolve:resolve,reject:reject,control:control};
      control.cancelQueued=function(){
        var index=waiting.indexOf(job);
        if(index>=0) {waiting.splice(index,1);control.cancelQueued=null;reject(aborted());}
      };
      waiting.push(job);pump();
    });
  }
  function aborted() { var e=new Error('上传已暂停，可继续上传');e.paused=true;return e; }
  function alive(control) { if(control.cancelled) throw aborted(); }
  function sleep(ms,control) { return new Promise(function(resolve){setTimeout(resolve,ms);}).then(function(){alive(control);}); }
  function request(method,url,body,control,headers,onprogress) {
    return new Promise(function(resolve,reject){
      alive(control);
      var xhr=new XMLHttpRequest();control.xhr=xhr;
      xhr.open(method,url,true);xhr.timeout=180000;
      if(method!=='GET') xhr.setRequestHeader('X-CSRF-Token',FT.csrf());
      Object.keys(headers||{}).forEach(function(key){xhr.setRequestHeader(key,headers[key]);});
      if(onprogress) xhr.upload.onprogress=onprogress;
      xhr.onload=function(){
        control.xhr=null;
        var data;try{data=JSON.parse(xhr.responseText);}catch(e){data={};}
        if(xhr.status>=200&&xhr.status<300) {resolve(data);return;}
        var error=new Error(data.detail||data.error||'上传失败');error.status=xhr.status;reject(error);
      };
      xhr.onerror=xhr.ontimeout=function(){control.xhr=null;var e=new Error('网络中断，已完成分片已保留');e.network=true;reject(e);};
      xhr.onabort=function(){control.xhr=null;reject(aborted());};
      xhr.send(body);
    });
  }
  function fingerprint(file,control,row) {
    return new Promise(function(resolve,reject){
      alive(control);
      var worker=new Worker('/static/js/upload-worker.js?v=1');control.worker=worker;
      control.rejectHash=function(){worker.terminate();reject(aborted());};
      worker.onmessage=function(event){
        var data=event.data;
        if(data.progress!==undefined) row.message('正在校验文件 '+Math.round(data.progress*100)+'%');
        if(data.sha256||data.error) {
          worker.terminate();control.worker=null;control.rejectHash=null;
          if(data.error) reject(new Error(data.error));else resolve(data.sha256);
        }
      };
      worker.onerror=function(){worker.terminate();control.worker=null;control.rejectHash=null;reject(new Error('文件校验失败，请重新选择'));};
      worker.postMessage({file:file});
    });
  }
  async function retryRequest(method,url,body,control,headers) {
    for(var n=0;;n++) {
      alive(control);
      try{return await request(method,url,body,control,headers);}
      catch(e) {if(e.paused||(!e.network&&e.status!==409&&e.status!==408&&e.status<500)||n>=3) throw e;await sleep(1000*Math.pow(2,n),control);}
    }
  }
  async function transfer(file,options,control,row) {
    alive(control);
    if(!file.size) throw new Error('不能上传空文件');
    if(!control.sha256) control.sha256=await fingerprint(file,control,row);
    alive(control);
    var data;
    if(control.id) {
      try {data=await retryRequest('GET','/api/uploads/'+control.id,null,control);}
      catch(e) {if(e.status!==410&&e.status!==404) throw e;control.id=null;}
      if(data&&data.file) return data.file;
      if(data&&data.upload.state!=='pending') {control.id=null;data=null;}
    }
    if(!control.id) {
      data=await retryRequest('POST','/api/uploads',JSON.stringify({name:file.name,size:file.size,
        sha256:control.sha256,expires_hours:options.expires_hours,is_public:options.is_public}),control,{'Content-Type':'application/json'});
      control.id=data.upload.id;
    }
    var upload=data.upload, offset=upload.offset, failures=0;
    if(offset) row.message('继续上传：已完成 '+FT.formatSize(offset));
    while(offset<file.size) {
      alive(control);
      var start=offset, blob=file.slice(start,Math.min(file.size,start+upload.chunk_size));
      var bytes=new Uint8Array(await blob.arrayBuffer());alive(control);
      var checksum=new FTUploadHash().update(bytes).hex();
      var since=Date.now();
      try {
        data=await request('PATCH','/api/uploads/'+control.id,blob,control,
          {'Content-Type':'application/octet-stream','Upload-Offset':String(start),'X-Chunk-SHA256':checksum},function(event){
            if(event.lengthComputable&&!control.cancelled) {
              var loaded=Math.min(file.size,start+event.loaded);
              var speed=event.loaded/Math.max(.05,(Date.now()-since)/1000);
              row.progress(loaded/file.size,speed,loaded,file.size);
            }
          });
        offset=data.upload.offset;failures=0;
      } catch(error) {
        if(error.paused||(!error.network&&error.status!==409&&error.status!==408&&error.status!==422&&error.status<500)||++failures>4) throw error;
        row.message('连接中断，正在恢复已完成的分片…');
        await sleep(1000*Math.pow(2,failures-1),control);
        // The server may have committed the chunk while its response was lost.
        data=await retryRequest('GET','/api/uploads/'+control.id,null,control);
        if(data.file) return data.file;
        offset=data.upload.offset;
      }
    }
    row.message('上传完成，正在校验文件完整性…');
    data=await retryRequest('POST','/api/uploads/'+control.id+'/complete',null,control);
    return data.file;
  }
  window.FTUploads={
    run:function(file,options,control,row){return slot(function(){return transfer(file,options,control,row);},control);},
    pause:function(control){control.cancelled=true;if(control.cancelQueued)control.cancelQueued();if(control.xhr)control.xhr.abort();if(control.rejectHash)control.rejectHash();},
    discard:async function(control){
      if(!control.id) return;
      var fresh={cancelled:false};
      await retryRequest('DELETE','/api/uploads/'+control.id,null,fresh);
      control.id=null;
    }
  };
})();
