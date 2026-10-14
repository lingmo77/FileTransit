const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
async function main() {
  const ctx={window:{},Promise,setTimeout,clearTimeout,
    Worker:class {postMessage(){} terminate(){}},FT:{}};
  vm.createContext(ctx);vm.runInContext(fs.readFileSync('app/static/js/upload-client.js','utf8'),ctx);
  const controls=[{cancelled:false},{cancelled:false},{cancelled:false}];
  const jobs=controls.map(control=>ctx.window.FTUploads.run({size:1},{},control,{message(){}}));
  const outcomes=jobs.map(job=>job.then(()=>null,error=>error));
  await new Promise(resolve=>setTimeout(resolve,10));
  ctx.window.FTUploads.pause(controls[2]);
  assert((await outcomes[2]).paused,'queued cancellation must settle immediately');
  ctx.window.FTUploads.pause(controls[0]);ctx.window.FTUploads.pause(controls[1]);
  assert((await outcomes[0]).paused);assert((await outcomes[1]).paused);
  console.log('Two-file concurrency and immediate queued/hash cancellation passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
