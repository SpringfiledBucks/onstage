export interface ArtworkStatus {supported?:boolean;registered?:boolean;busy?:boolean;total?:number;completed?:number;failed?:number;cached?:number;cachedBytes?:number;error?:string;message?:string;updateAvailable?:boolean}
interface ArtworkAPI {
    init(options?:{onStatus?:(status:ArtworkStatus)=>void}):Promise<ArtworkStatus>;
    downloadArtwork():Promise<ArtworkStatus>;
    cancelDownload():Promise<ArtworkStatus>;
    clearArtwork():Promise<ArtworkStatus>;
    getStatus():ArtworkStatus;
    refreshStatus():Promise<ArtworkStatus>;
    activateUpdate?():Promise<ArtworkStatus>;
    unsubscribe?(listener:(status:ArtworkStatus)=>void):void;
}
declare global {interface Window {OnstagePWA?:ArtworkAPI;OnstageArtworkCache?:ArtworkAPI}}
export function artworkCache(): ArtworkAPI | undefined {
    if(window.OnstageArtworkCache) return window.OnstageArtworkCache;
    const native=window.OnstagePWA;
    if(!native) return undefined;
    const listeners=new Set<(status:ArtworkStatus)=>void>();
    let initialization:Promise<ArtworkStatus>|undefined;
    const shared:ArtworkAPI={
        init(options={}){
            if(options.onStatus){listeners.add(options.onStatus);options.onStatus(native.getStatus())}
            return initialization ||= native.init({onStatus:status=>{for(const listener of listeners){try{listener({...status})}catch{ /* A view cannot interrupt cache work. */ }}}});
        },
        downloadArtwork:()=>native.downloadArtwork(),cancelDownload:()=>native.cancelDownload(),clearArtwork:()=>native.clearArtwork(),
        getStatus:()=>native.getStatus(),refreshStatus:()=>native.refreshStatus(),unsubscribe:listener=>listeners.delete(listener),
        activateUpdate:()=>native.activateUpdate?.() || native.refreshStatus()
    };
    window.OnstageArtworkCache=shared;
    return shared;
}
