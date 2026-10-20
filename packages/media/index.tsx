import React, {ImgHTMLAttributes} from 'react';

// Public artwork only. Identity, API responses and conversation data never enter this map.
const assets = new Map<string, string>();
let loading: Promise<void> | undefined;
function localImage(value: unknown): string | undefined {
    if(typeof value !== 'string' || !value) return undefined;
    try {
        const url = new URL(value, location.origin);
        return url.origin === location.origin && url.pathname.startsWith('/static/') && !url.search && !url.hash ? url.pathname : undefined;
    } catch { return undefined; }
}
export function mediaUrl(value: unknown): string | undefined {
    const original = localImage(value);
    return original ? assets.get(original) || original : undefined;
}
export function loadPublicMedia(): Promise<void> {
    return loading ||= (async()=>{
        const controller = new AbortController();
        const timeout = setTimeout(()=>controller.abort(),1200);
        try {
            const response = await fetch('/static/assets-manifest.json',{credentials:'omit',cache:'no-cache',signal:controller.signal});
            if(!response.ok) return;
            const data = await response.json();
            if(data.schema_version !== 1 || !data.assets || typeof data.assets !== 'object') return;
            for(const [original, candidate] of Object.entries(data.assets)) {
                const url=(candidate as {url?:unknown})?.url;
                if(original.startsWith('/static/assets/') && localImage(original) && typeof url==='string' && /^\/static\/assets\/optimized\/[a-f0-9]{64}\.webp$/.test(url)) assets.set(original,url);
            }
        } catch { /* Original URLs remain the compatible fallback if the manifest is unavailable. */ }
        finally { clearTimeout(timeout); }
    })();
}
export function Artwork({src,onError,...props}:ImgHTMLAttributes<HTMLImageElement>) {
    const original=localImage(src);
    const resolved=mediaUrl(src);
    if(!resolved) return null;
    return <img {...props} src={resolved} onError={event=>{
        const image=event.currentTarget;
        if(original && image.getAttribute('src')!==original) image.src=original;
        else onError?.(event);
    }}/>;
}
