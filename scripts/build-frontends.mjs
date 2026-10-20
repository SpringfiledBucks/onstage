import {build} from 'vite';
import {cp,mkdir,writeFile,readFile,rm} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const config='window.ONSTAGE_CONFIG = window.ONSTAGE_CONFIG || { apiBaseUrl: "", wsBaseUrl: "" };\n';
for(const name of ['web','admin']){
 const outDir=path.join(root,'build',name);
 await build({configFile:false,root:path.join(root,'apps',name),base:'/',build:{outDir,emptyOutDir:true},publicDir:false});
 await writeFile(path.join(outDir,'config.js'),config);
 if(name==='web'){
   await cp(path.join(root,'static'),path.join(outDir,'static'),{recursive:true});
   await cp(path.join(root,'static','sw.js'),path.join(outDir,'sw.js'));
   await mkdir(path.join(outDir,'play'),{recursive:true});
   const legacy=(await readFile(path.join(root,'static','index.html'),'utf8')).replace('<script src="/static/theme.js">','<script src="/config.js"></script><script src="/static/theme.js">');
   await writeFile(path.join(outDir,'play','index.html'),legacy);
 }
}
console.log('Built independent web and admin frontends into build/web and build/admin. Configure SPA fallback except /play, /static and /config.js.');
