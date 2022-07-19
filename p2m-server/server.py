from pydoc import describe
from fastapi import FastAPI, File, UploadFile, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import sys
import inspect

currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0, parentdir)

# import main as p2m

app = FastAPI()

# Fixing CORS errors: https://stackoverflow.com/questions/65635346/how-can-i-enable-cors-in-fastapi
origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Homepage
@app.get("/")
async def mainPage():
    content = """
            <body>
                <center>
                <h1>Text2Mesh</h1>
                <form action="/uploadMesh" enctype="multipart/form-data" method="post">
                    <input name="file" type="file" >
                    <input type="submit">
                </form>
                </center>
            </body>
    """
    return HTMLResponse(content=content)

@app.get("/uploadMesh")
async def threeJSMeshViewer(
    uploaded_mesh: UploadFile = File(description="Mesh"),
):
    request_object_content = await uploaded_mesh.read()
    # Visualize uploaded_mesh in Three.js Viewer plz
    OLIVIAS_HTML_TEMPLATE = "<html><body>hemlo</body></html>"
    return OLIVIAS_HTML_TEMPLATE

@app.post("/t2m")
async def t2m_MeshReturner(
    file: UploadFile = File(description="Mesh"),
    text: str = Form()
):
    inf_mesh = t2m(file)
    return FileResponse(inf_mesh, media_type="model/obj", filename="t2m_mesh")

def t2m_args():
    args = {}
    return args

def t2m(file, text):
    args = t2m_args()
    inf_mesh = p2m.run_branched(args, file, text)
    return inf_mesh