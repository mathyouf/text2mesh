import clip
from tqdm import tqdm
# Kaolin as Differentiable Renderer
import kaolin.ops.mesh
import kaolin as kal
import torch
from neural_style_field import NeuralStyleField
from utils import device
from render import Renderer
from mesh import Mesh
from utils import clip_model
from Normalization import MeshNormalizer
from utils import preprocess, add_vertices, sample_bary
import numpy as np
import random
import copy
import torchvision
import os
from PIL import Image
import argparse
from pathlib import Path
from torchvision import transforms
import numpy as np

def run_branched(args):
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    # Constrain all sources of randomness
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

    objbase, extension = os.path.splitext(os.path.basename(args.obj_path))
    # Check that isn't already done
    if (not args.overwrite) and os.path.exists(os.path.join(args.output_dir, "loss.png")) and \
            os.path.exists(os.path.join(args.output_dir, f"{objbase}_final.obj")):
        print(f"Already done with {args.output_dir}")
        exit()
    elif args.overwrite and os.path.exists(os.path.join(args.output_dir, "loss.png")) and \
            os.path.exists(os.path.join(args.output_dir, f"{objbase}_final.obj")):
        import shutil
        for filename in os.listdir(args.output_dir):
            file_path = os.path.join(args.output_dir, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print('Failed to delete %s. Reason: %s' % (file_path, e))

    ### Kaolin DIB-R (Differentiable Renderer)
    # Default Image Size: 224x224

    # Create the Kaolin DIB-R Renderer - https://kaolin.readthedocs.io/en/latest/modules/kaolin.render.mesh.html#kaolin.render.mesh.dibr_rasterization
    render = Renderer()
    # The Base Mesh that is deformed, with kal.io.obj.import_mesh() - https://kaolin.readthedocs.io/en/latest/modules/kaolin.io.obj.html
    # This mesh initializes with an all Blue texture map
    mesh = Mesh(args.obj_path)
    # Normalizes mesh (scales and centers)
    MeshNormalizer(mesh)()
    # What is prior_color ? - Seems to be grey
    prior_color = torch.full(size=(mesh.faces.shape[0], 3, 3), fill_value=0.5, device=device)

    background = None
    if args.background is not None:
        assert len(args.background) == 3
        background = torch.tensor(args.background).to(device)

    losses = []

    n_augs = args.n_augs
    dir = args.output_dir
    # Normalization vectors (constants) set empirically. The model is found to be robust to slightly different normalization constants - https://github.com/openai/CLIP/issues/20
    # Noramlize(mean, std_dev) - Normalization Intuition: https://discuss.pytorch.org/t/how-does-torchvision-transforms-normalize-work/57670
    clip_normalizer = transforms.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711))
    # CLIP Transform - List of transforms to apply to an input image (rendered_images)
    clip_transform = transforms.Compose([
        transforms.Resize((224, 224)),
        clip_normalizer
    ])

    # Augmentation settings
    augment_transform = transforms.Compose([
        # This doesn't seem to be cropping at all - Crop a bit and then resize back to 224
        transforms.RandomResizedCrop(224, scale=(1, 1)),
        # Does a bit of a rotation/angling of an render - Why do this when you can already get every 3D angle???
        transforms.RandomPerspective(fill=1, p=0.8, distortion_scale=0.5),
        # Re-normalize augmented images to CLIP Norm Constants
        clip_normalizer
    ])

    # Augmentations for normal network
    if args.cropforward :
        curcrop = args.normmincrop
    else:
        curcrop = args.normmaxcrop
    normaugment_transform = transforms.Compose([
        # 10% crops on the renders
        # Enforces texture/deform scale/density regularity
        transforms.RandomResizedCrop(224, scale=(curcrop, curcrop)),
        transforms.RandomPerspective(fill=1, p=0.8, distortion_scale=0.5),
        clip_normalizer
    ])
    cropiter = 0
    cropupdate = 0
    if args.normmincrop < args.normmaxcrop and args.cropsteps > 0:
        cropiter = round(args.n_iter / (args.cropsteps + 1))
        cropupdate = (args.maxcrop - args.mincrop) / cropiter

        if not args.cropforward:
            cropupdate *= -1

    # Displacement-only augmentations
    displaugment_transform = transforms.Compose([
        transforms.RandomResizedCrop(224, scale=(args.normmincrop, args.normmincrop)),
        transforms.RandomPerspective(fill=1, p=0.8, distortion_scale=0.5),
        clip_normalizer
    ])

    normweight = 1.0

    # MLP Settings
    input_dim = 6 if args.input_normals else 3
    if args.only_z:
        input_dim = 1
    # Get Neural Style network, the thing that learns to create the target mesh
    mlp = NeuralStyleField(args.sigma, args.depth, args.width, 'gaussian', args.colordepth, args.normdepth,
                                args.normratio, args.clamp, args.normclamp, niter=args.n_iter,
                                progressive_encoding=args.pe, input_dim=input_dim, exclude=args.exclude).to(device)
    mlp.reset_weights()
    # Setup the optimzier to calcualte gradients and backpropogate them for the parameters of MLP (multilayer perceptron)
    optim = torch.optim.Adam(mlp.parameters(), args.learning_rate, weight_decay=args.decay)
    activate_scheduler = args.lr_decay < 1 and args.decay_step > 0 and not args.lr_plateau
    if activate_scheduler:
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optim, step_size=args.decay_step, gamma=args.lr_decay)
    # args.no_prompt = False => Resolves True
    if not args.no_prompt:
        # True - Default: 'a pig with pants'
        if args.prompt:
            # ['a pig with pants', 'sakura tree island'] => 'a pig with pants sakura tree island'
            prompt = ' '.join(args.prompt)
            # Tokenize the above string - prompt_token.Size = [1, 77]
            prompt_token = clip.tokenize([prompt]).to(device)
            # From: clip_model, preprocess = clip.load('ViT-B/32', device, jit=False)
            # ViT - Vision Transformer
            encoded_text = clip_model.encode_text(prompt_token)

            # Save prompt
            with open(os.path.join(dir, prompt), "w") as f:
                f.write("")

            # Same with normprompt
            norm_encoded = encoded_text
    # args.normprompt = None - Resolves False
    if args.normprompt is not None:
        prompt = ' '.join(args.normprompt)
        prompt_token = clip.tokenize([prompt]).to(device)
        norm_encoded = clip_model.encode_text(prompt_token)

        # Save prompt
        with open(os.path.join(dir, f"NORM {prompt}"), "w") as f:
            f.write("")

    # Uses image vector from CLIP as TARGET rather than Text Input
    if args.image:
        img = Image.open(args.image)
        img = preprocess(img).to(device)
        encoded_image = clip_model.encode_image(img.unsqueeze(0))
        if args.no_prompt:
            norm_encoded = encoded_image

    loss_check = None
    vertices = copy.deepcopy(mesh.vertices)
    network_input = copy.deepcopy(vertices)
    if args.symmetry == True:
        # network_input.size (n_verts, 3) -> 3 = [x,y,z]
        # Make them symmetrical along the z axis
        network_input[:,2] = torch.abs(network_input[:,2])

    if args.standardize == True:
        # Each channel into z-score
        network_input = (network_input - torch.mean(network_input, dim=0))/torch.std(network_input, dim=0)

    ### Now we start descending the iteratively (i) calculated gradients!!! - https://en.wikipedia.org/wiki/Gradient_descent ###
    for i in tqdm(range(args.n_iter)):
        optim.zero_grad()

        sampled_mesh = mesh
        ### Updates sampled_mesh directly ###
        # pred_rgb, pred_normal = mlp(network_input)
        # sampled_mesh.face_attributes = prior_color + kaolin.ops.mesh.index_vertices_by_faces(pred_rgb.unsqueeze(0),sampled_mesh.faces)
        # sampled_mesh.vertices = vertices + sampled_mesh.vertex_normals * pred_normal
        update_mesh(mlp, network_input, prior_color, sampled_mesh, vertices)
        ### Renders images from the sampled_mesh ###
        rendered_images, elev, azim = render.render_front_views(sampled_mesh, num_views=args.n_views,
                                                                show=args.show,
                                                                center_azim=args.frontview_center[0],
                                                                center_elev=args.frontview_center[1],
                                                                std=args.frontview_std,
                                                                return_views=True,
                                                                background=background)
        # n_augs == 1
        if n_augs == 0:
            clip_image = clip_transform(rendered_images)
            encoded_renders = clip_model.encode_image(clip_image)
            if not args.no_prompt:
                loss = torch.mean(torch.cosine_similarity(encoded_renders, encoded_text))

        # Check augmentation steps
        if args.cropsteps != 0 and cropupdate != 0 and i != 0 and i % args.cropsteps == 0:
            curcrop += cropupdate
            # print(curcrop)
            normaugment_transform = transforms.Compose([
                transforms.RandomResizedCrop(224, scale=(curcrop, curcrop)),
                transforms.RandomPerspective(fill=1, p=0.8, distortion_scale=0.5),
                clip_normalizer
            ])

        # We calculate loss based on the augmentations
        if n_augs > 0:
            loss = 0.0
            for _ in range(n_augs):
                # Create Augmented Images (same as rendered_images (5))
                augmented_image = augment_transform(rendered_images)
                # Create CLIP encodings of the augmented images
                encoded_renders = clip_model.encode_image(augmented_image)
                # This resolves True (because there is NOT no prompt)
                if not args.no_prompt:
                    if args.prompt:
                        if args.clipavg == "view":
                            # If we have more than one prompt
                            if encoded_text.shape[0] > 1:
                                # Get the cosine similarity between: The mean of the encoded renders and the mean of the encoded text
                                etsize = encoded_text.size()
                                print(f'encoded_text: {encoded_text} {etsize}')
                                loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0),
                                                                torch.mean(encoded_text, dim=0), dim=0)
                            else:
                                loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0, keepdim=True),
                                                                encoded_text)
                        else:
                            loss -= torch.mean(torch.cosine_similarity(encoded_renders, encoded_text))
                if args.image:
                    if encoded_image.shape[0] > 1:
                        loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0),
                                                        torch.mean(encoded_image, dim=0), dim=0)
                    else:
                        loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0, keepdim=True),
                                                        encoded_image)
                    # if args.image:
                    #     loss -= torch.mean(torch.cosine_similarity(encoded_renders,encoded_image))
        if args.splitnormloss:
            for param in mlp.mlp_normal.parameters():
                param.requires_grad = False
        loss.backward(retain_graph=True)

        # optim.step()

        # with torch.no_grad():
        #     losses.append(loss.item())

        # Normal augment transform
        # loss = 0.0
        if args.n_normaugs > 0:
            normloss = 0.0
            for _ in range(args.n_normaugs):
                augmented_image = normaugment_transform(rendered_images)
                encoded_renders = clip_model.encode_image(augmented_image)
                if not args.no_prompt:
                    if args.prompt:
                        if args.clipavg == "view":
                            if norm_encoded.shape[0] > 1:
                                normloss -= normweight * torch.cosine_similarity(torch.mean(encoded_renders, dim=0),
                                                                                 torch.mean(norm_encoded, dim=0),
                                                                                 dim=0)
                            else:
                                normloss -= normweight * torch.cosine_similarity(
                                    torch.mean(encoded_renders, dim=0, keepdim=True),
                                    norm_encoded)
                        else:
                            normloss -= normweight * torch.mean(
                                torch.cosine_similarity(encoded_renders, norm_encoded))
                if args.image:
                    if encoded_image.shape[0] > 1:
                        loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0),
                                                        torch.mean(encoded_image, dim=0), dim=0)
                    else:
                        loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0, keepdim=True),
                                                        encoded_image)
                    # if args.image:
                    #     loss -= torch.mean(torch.cosine_similarity(encoded_renders,encoded_image))
            if args.splitnormloss:
                for param in mlp.mlp_normal.parameters():
                    param.requires_grad = True
            if args.splitcolorloss:
                for param in mlp.mlp_rgb.parameters():
                    param.requires_grad = False
            if not args.no_prompt:
                normloss.backward(retain_graph=True)

        # Also run separate loss on the uncolored displacements
        if args.geoloss:
            default_color = torch.zeros(len(mesh.vertices), 3).to(device)
            default_color[:, :] = torch.tensor([0.5, 0.5, 0.5]).to(device)
            sampled_mesh.face_attributes = kaolin.ops.mesh.index_vertices_by_faces(default_color.unsqueeze(0),
                                                                                   sampled_mesh.faces)
            geo_renders, elev, azim = render.render_front_views(sampled_mesh, num_views=args.n_views,
                                                                show=args.show,
                                                                center_azim=args.frontview_center[0],
                                                                center_elev=args.frontview_center[1],
                                                                std=args.frontview_std,
                                                                return_views=True,
                                                                background=background)
            if args.n_normaugs > 0:
                normloss = 0.0
                ### avgview != aug
                for _ in range(args.n_normaugs):
                    augmented_image = displaugment_transform(geo_renders)
                    encoded_renders = clip_model.encode_image(augmented_image)
                    if norm_encoded.shape[0] > 1:
                        normloss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0),
                                                            torch.mean(norm_encoded, dim=0), dim=0)
                    else:
                        normloss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0, keepdim=True),
                                                            norm_encoded)
                    if args.image:
                        if encoded_image.shape[0] > 1:
                            loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0),
                                                            torch.mean(encoded_image, dim=0), dim=0)
                        else:
                            loss -= torch.cosine_similarity(torch.mean(encoded_renders, dim=0, keepdim=True),
                                                            encoded_image)  # if args.image:
                        #     loss -= torch.mean(torch.cosine_similarity(encoded_renders,encoded_image))
                # if not args.no_prompt:
                normloss.backward(retain_graph=True)
        optim.step()

        for param in mlp.mlp_normal.parameters():
            param.requires_grad = True
        for param in mlp.mlp_rgb.parameters():
            param.requires_grad = True

        if activate_scheduler:
            lr_scheduler.step()

        with torch.no_grad():
            losses.append(loss.item())

        # Adjust normweight if set
        if args.decayfreq is not None:
            if i % args.decayfreq == 0:
                normweight *= args.cropdecay

        if i % 100 == 0:
            report_process(args, dir, i, loss, loss_check, losses, rendered_images)

    export_final_results(args, dir, losses, mesh, mlp, network_input, vertices)


def report_process(args, dir, i, loss, loss_check, losses, rendered_images):
    print('iter: {} loss: {}'.format(i, loss.item()))
    # Re-render images at higher resolution
    re_rendered_images, elev_re, azim_re = render.render_front_views(sampled_mesh, num_views=args.n_views,
                                                        show=args.show,
                                                        center_azim=args.frontview_center[0],
                                                        center_elev=args.frontview_center[1],
                                                        std=args.frontview_std,
                                                        return_views=True,
                                                        background=background)
    torchvision.utils.save_image(re_rendered_images, os.path.join(dir, 'iter_{}.jpg'.format(i)))
    if args.lr_plateau and loss_check is not None:
        new_loss_check = np.mean(losses[-100:])
        # If avg loss increased or plateaued then reduce LR
        if new_loss_check >= loss_check:
            for g in torch.optim.param_groups:
                g['lr'] *= 0.5
        loss_check = new_loss_check

    elif args.lr_plateau and loss_check is None and len(losses) >= 100:
        loss_check = np.mean(losses[-100:])


def export_final_results(args, dir, losses, mesh, mlp, network_input, vertices):
    with torch.no_grad():
        pred_rgb, pred_normal = mlp(network_input)
        pred_rgb = pred_rgb.detach().cpu()
        pred_normal = pred_normal.detach().cpu()

        torch.save(pred_rgb, os.path.join(dir, f"colors_final.pt"))
        torch.save(pred_normal, os.path.join(dir, f"normals_final.pt"))

        base_color = torch.full(size=(mesh.vertices.shape[0], 3), fill_value=0.5)
        final_color = torch.clamp(pred_rgb + base_color, 0, 1)

        torchvision.utils.save_image(mesh.texture_map[0], os.path.join(dir, f"img1.png"))

        mesh.vertices = vertices.detach().cpu() + mesh.vertex_normals.detach().cpu() * pred_normal

        objbase, extension = os.path.splitext(os.path.basename(args.obj_path))

        mesh.export(os.path.join(dir, f"{objbase}_final.obj"), color=final_color)

        # Run renders
        if args.save_render:
            save_rendered_results(args, dir, final_color, mesh)

        # Save final losses
        torch.save(torch.tensor(losses), os.path.join(dir, "losses.pt"))


def save_rendered_results(args, dir, final_color, mesh):
    default_color = torch.full(size=(mesh.vertices.shape[0], 3), fill_value=0.5, device=device)
    mesh.face_attributes = kaolin.ops.mesh.index_vertices_by_faces(default_color.unsqueeze(0),
                                                                   mesh.faces.to(device))
    kal_render = Renderer(
        camera=kal.render.camera.generate_perspective_projection(np.pi / 4, 1280 / 720).to(device),
        dim=(1280, 720))
    MeshNormalizer(mesh)()
    img, mask = kal_render.render_single_view(mesh, args.frontview_center[1], args.frontview_center[0],
                                              radius=2.5,
                                              background=torch.tensor([1, 1, 1]).to(device).float(),
                                              return_mask=True)
    img = img[0].cpu()
    mask = mask[0].cpu()
    # Manually add alpha channel using background color
    alpha = torch.ones(img.shape[1], img.shape[2])
    alpha[torch.where(mask == 0)] = 0
    img = torch.cat((img, alpha.unsqueeze(0)), dim=0)
    img = transforms.ToPILImage()(img)
    img.save(os.path.join(dir, f"init_cluster.png"))
    MeshNormalizer(mesh)()
    # Vertex colorings
    mesh.face_attributes = kaolin.ops.mesh.index_vertices_by_faces(final_color.unsqueeze(0).to(device),
                                                                   mesh.faces.to(device))
    img, mask = kal_render.render_single_view(mesh, args.frontview_center[1], args.frontview_center[0],
                                              radius=2.5,
                                              background=torch.tensor([1, 1, 1]).to(device).float(),
                                              return_mask=True)
    img = img[0].cpu()
    mask = mask[0].cpu()
    # Manually add alpha channel using background color
    alpha = torch.ones(img.shape[1], img.shape[2])
    alpha[torch.where(mask == 0)] = 0
    img = torch.cat((img, alpha.unsqueeze(0)), dim=0)
    img = transforms.ToPILImage()(img)
    img.save(os.path.join(dir, f"final_cluster.png"))


def update_mesh(mlp, network_input, prior_color, sampled_mesh, vertices):
    # Get the updated RGB and Normal distances from the Neural Style Field
    pred_rgb, pred_normal = mlp(network_input)
    # Update the mesh with the new color
    sampled_mesh.face_attributes = prior_color + kaolin.ops.mesh.index_vertices_by_faces(
        pred_rgb.unsqueeze(0),
        sampled_mesh.faces)
    # Update the mesh with the new normal
    # Add to the vertex the normal matrix multiplied by the pred_normal
    a = sampled_mesh.vertices.size()
    b = vertices.size()
    c = sampled_mesh.vertex_normals.size()
    print(f'sampled_mesh.vertices: {a}, - vertices: {b} - sampled_mesh.vertex_normals: {c}')
    sampled_mesh.vertices = vertices + sampled_mesh.vertex_normals * pred_normal
    # Normalize it (Scale + Center)
    MeshNormalizer(sampled_mesh)()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    ### Users should be able to change this by selecting different objects ###
    parser.add_argument('--obj_path', type=str, default='meshes/mesh1.obj')
    ### Users should be able to change this by entering different prompts ###
    parser.add_argument('--prompt', nargs="+", default='a pig with pants')
    parser.add_argument('--normprompt', nargs="+", default=None)
    parser.add_argument('--promptlist', nargs="+", default=None)
    parser.add_argument('--normpromptlist', nargs="+", default=None)
    parser.add_argument('--image', type=str, default=None)
    parser.add_argument('--output_dir', type=str, default='round2/alpha5')
    parser.add_argument('--traintype', type=str, default="shared")
    parser.add_argument('--sigma', type=float, default=10.0)
    parser.add_argument('--normsigma', type=float, default=10.0)
    parser.add_argument('--depth', type=int, default=4)
    parser.add_argument('--width', type=int, default=256)
    parser.add_argument('--colordepth', type=int, default=2)
    parser.add_argument('--normdepth', type=int, default=2)
    parser.add_argument('--normwidth', type=int, default=256)
    parser.add_argument('--learning_rate', type=float, default=0.0005)
    parser.add_argument('--normal_learning_rate', type=float, default=0.0005)
    parser.add_argument('--decay', type=float, default=0)
    parser.add_argument('--lr_decay', type=float, default=1)
    parser.add_argument('--lr_plateau', action='store_true')
    parser.add_argument('--no_pe', dest='pe', default=True, action='store_false')
    parser.add_argument('--decay_step', type=int, default=100)
    parser.add_argument('--n_views', type=int, default=5)
    parser.add_argument('--n_augs', type=int, default=0)
    parser.add_argument('--n_normaugs', type=int, default=0)
    parser.add_argument('--n_iter', type=int, default=6000)
    parser.add_argument('--encoding', type=str, default='gaussian')
    parser.add_argument('--normencoding', type=str, default='xyz')
    parser.add_argument('--layernorm', action="store_true")
    parser.add_argument('--run', type=str, default=None)
    parser.add_argument('--gen', action='store_true')
    parser.add_argument('--clamp', type=str, default="tanh")
    parser.add_argument('--normclamp', type=str, default="tanh")
    parser.add_argument('--normratio', type=float, default=0.1)
    parser.add_argument('--frontview', action='store_true')
    parser.add_argument('--no_prompt', default=False, action='store_true')
    parser.add_argument('--exclude', type=int, default=0)

    parser.add_argument('--frontview_std', type=float, default=8)
    parser.add_argument('--frontview_center', nargs=2, type=float, default=[0., 0.])
    parser.add_argument('--clipavg', type=str, default=None)
    parser.add_argument('--geoloss', action="store_true")
    parser.add_argument('--samplebary', action="store_true")
    parser.add_argument('--promptviews', nargs="+", default=None)
    parser.add_argument('--mincrop', type=float, default=1)
    parser.add_argument('--maxcrop', type=float, default=1)
    parser.add_argument('--normmincrop', type=float, default=0.1)
    parser.add_argument('--normmaxcrop', type=float, default=0.1)
    parser.add_argument('--splitnormloss', action="store_true")
    parser.add_argument('--splitcolorloss', action="store_true")
    parser.add_argument("--nonorm", action="store_true")
    parser.add_argument('--cropsteps', type=int, default=0)
    parser.add_argument('--cropforward', action='store_true')
    parser.add_argument('--cropdecay', type=float, default=1.0)
    parser.add_argument('--decayfreq', type=int, default=None)
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--show', action='store_true')
    parser.add_argument('--background', nargs=3, type=float, default=None)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--save_render', action="store_true")
    parser.add_argument('--input_normals', default=False, action='store_true')
    parser.add_argument('--symmetry', default=False, action='store_true')
    parser.add_argument('--only_z', default=False, action='store_true')
    parser.add_argument('--standardize', default=False, action='store_true')

    args = parser.parse_args()

    run_branched(args)
