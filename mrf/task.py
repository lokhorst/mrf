import os
import sys
import gc
import copy
import yaml
import logging

import numpy as np
import matplotlib.pyplot as plt

from astropy import wcs
from astropy.io import fits
from astropy.table import Table, Column, hstack, vstack

import warnings
warnings.filterwarnings("ignore")

class Config(object):
    """
    Configuration class.
    """
    def __init__(self, d):
        for a, b in d.items():
            if isinstance(b, (list, tuple)):
                setattr(self, a, [Config(x) if isinstance(x, dict) else x for x in b])
            else:
                setattr(self, a, Config(b) if isinstance(b, dict) else b)
        Config.config = d
    
    def complete_config(self):
        """
        This function fill vacant parameters in the config file with default values. 

        Parameters:
            config (Config class): configuration of the MRF task.
        Returns:
            config (Config class)
        """
        # sex
        default_sex = {
            'b': 64,
            'f': 3,
            'sigma': 3.0,
            'minarea': 2,
            'deblend_cont': 0.005,
            'deblend_nthresh': 32,
            'sky_subtract': True,
            'flux_aper': [3, 6],
            'show_fig': False
        }
        for name in default_sex.keys():
            if not name in self.sex.__dict__.keys():
                setattr(self.sex, name, default_sex[name])
        
        # fluxmodel
        default = {'gaussian_radius': 1.5,
                'gaussian_threshold': 0.05,
                'unmask_lowsb': False,
                'sb_lim': 26.0,
                'unmask_ratio': 3,
                'interp': 'iraf',
                'minarea': 25
                }
        for name in default.keys():
            if not name in self.fluxmodel.__dict__.keys():
                setattr(self.fluxmodel, name, default[name])

        # kernel
        default = {
            'kernel_size': 8,
            'kernel_edge': 1,
            'nkernel': 25,
            'circularize': False,
            'show_fig': True
        }
        for name in default.keys():
            if not name in self.kernel.__dict__.keys():
                setattr(self.kernel, name, default[name])
        
        # starhalo
        default = {
            'fwhm_lim': 200,
            'padsize': 50,
            'edgesize': 5,
            'b': 32,
            'f': 3,
            'sigma': 3.5,
            'minarea': 3,
            'deblend_cont': 0.003,
            'deblend_nthresh': 32,
            'sky_subtract': True,
            'flux_aper': [3, 6],
            'mask_contam': True,
            'interp': 'iraf',
            'cval': 'nan'
        }
        for name in default.keys():
            if not name in self.starhalo.__dict__.keys():
                setattr(self.starhalo, name, default[name])

        # WIDE_PSF
        default = {
            'frac': 0.3,
            'fwhm': 2.28,
            'beta': 3,
            'n_s': [3.44, 2.89, 2.07, 4],
            'theta_s': [5, 64.6, 117.5, 1200]
        }
        if 'wide_psf' not in self.__dict__:
            self.wide_psf = Config({})
        for name in default.keys():
            if not name in self.wide_psf.__dict__.keys():
                setattr(self.wide_psf, name, default[name])

        # Clean
        default = {
            'clean_img': True,
            'clean_file': False,
            'replace_with_noise': False,
            'gaussian_radius': 1.5,
            'gaussian_threshold': 0.003,
            'bright_lim': 16.5,
            'r': 8.0
        }
        for name in default.keys():
            if not name in self.clean.__dict__.keys():
                setattr(self.clean, name, default[name])

class Results():
    """
    Results class. Other attributes will be added by ``setattr()``.
    """
    def __init__(self, config):
        self.config = config
    
class MrfTask():
    '''
    MRF task class. This class implements `mrf`, with wide-angle PSF incorporated.
    '''
    def __init__(self, config_file):
        """
        Initialize ``MrfTask`` class. 

        Parameters:
            config_file (str): the directory of configuration YAML file.
        Returns:
            None
        """
        # Open configuration file
        with open(config_file, 'r') as ymlfile:
            cfg = yaml.safe_load(ymlfile)
            config = Config(cfg)
            config.complete_config() # auto-complete absent keywords
        self.config_file = config_file
        self.config = config

    def set_logger(self, output_name='mrf', verbose=True):
        """
        Set logger for ``MrfTask``. The logger will record the time and each output. The log file will be saved locally.

        Parameters:
            verbose (bool): If False, the logger will be silent. 

        Returns:
            logger (``logging.logger`` object)
        """
        if verbose:
            log_filename = output_name + '.log'
            logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO, 
                                handlers=[logging.StreamHandler(sys.stdout),
                                          logging.FileHandler(log_filename, mode='w')])
            self.logger = logging.getLogger(log_filename)                          
        else:
            logger = logging.getLogger('mylogger')
            logger.propagate = False
            self.logger = logger
        return self.logger

    def run(self, dir_lowres, dir_hires_b, dir_hires_r, certain_gal_cat, 
            wide_psf=True, output_name='mrf', verbose=True, skip_resize=False, 
            skip_SE=False, skip_mast=False, mast_catalog=None):
        """
        Run MRF task.

        Parameters:
            dir_lowres (string): directory of input low-resolution image.
            dir_hires_b (string): directory of input high-resolution 
                blue-band image (typically g-band).
            dir_hires_r (string): directory of input high-resolution 
                red-band image (typically r-band).
            certain_gal_cat (string): directory of a catalog (in ascii format) which contains 
                RA and DEC of galaxies which you want to retain during MRF.
            wide_psf (bool): whether subtract bright stars using the wide-PSF of **Dragonfly**. 
                See Q. Liu et al. (in prep.) for details. 
            output_name (string): which will be the prefix of output files.
            verbose (bool): If True, it will make a log file recording the process. 
            skip_resize (bool): If True, the code will not `zoom` the images again but 
                use the resized images under the current directory. 
                This is designed for the case when you need to tweak parameters.
            skip_SE (bool): If True, the code will not repeat running SExtractor on two-bands high-res images, 
                but use the existing flux model under the current directory. 
                This is designed for the case when you need to tweak parameters.
            skip_mast (bool): Just for ``wide_psf=True`` mode. If True, the code will not 
                repeat retrieving Pan-STARRS catalog from MAST server, 
                but use the existing catalog under the current directory. 
                This is designed for the case when you need to tweak parameters.
            mast_catalog (str): The directory of the Pan-STARRS catalog. Just for ``wide_psf=True`` mode. 
                If "None", the code will use "./_ps1_cat.fits" as the filename.
                This is designed for the case when you need to tweak parameters.

        Returns:
            results (`Results` class): containing key results of this task.
        
        """
        from astropy.coordinates import SkyCoord, match_coordinates_sky
        from astropy.convolution import convolve, Box2DKernel, Gaussian2DKernel
        import astropy.units as u
        from mrf.utils import (save_to_fits, Flux_Model, mask_out_stars, extract_obj, \
                            bright_star_mask, Autokernel, psf_bkgsub)
        from mrf.utils import seg_remove_obj, mask_out_certain_galaxy

        from mrf.display import display_single, SEG_CMAP, display_multiple, draw_circles
        from mrf.celestial import Celestial, Star
        from mrf.utils import Config
        from reproject import reproject_interp, reproject_exact

        config = self.config
        logger = self.set_logger(output_name=output_name, verbose=verbose)
        results = Results(config)
        
        assert (
            ((config.lowres.dataset.lower() != 'df' or config.lowres.dataset.lower() != 'dragonfly') and wide_psf == True) or wide_psf == False
            ), "Wide PSF subtraction is only available for Dragonfly data. Check your low-resolution images!"

        logger.info('Running Multi-Resolution Filtering (MRF) on "{0}" and "{1}" images!'.format(config.hires.dataset, config.lowres.dataset))
        setattr(results, 'lowres_name', config.lowres.dataset)
        setattr(results, 'hires_name', config.hires.dataset)
        setattr(results, 'output_name', output_name)
        
        # 1. subtract background of lowres, if desired
        assert isinstance(dir_lowres, str), 'Input "img_lowres" must be string!'
        hdu = fits.open(dir_lowres)
        lowres = Celestial(hdu[0].data, header=hdu[0].header)
        if config.lowres.sub_bkgval:
            if wide_psf == True:
                bkgval = float(getattr(config.wide_psf, 'bkgval', lowres.header['BACKVAL']))
            else:
                bkgval = float(lowres.header['BACKVAL'])
            logger.info('Subtract BACKVAL=%.1f of Dragonfly image', bkgval)
            lowres.image -= bkgval
        hdu.close()
        setattr(results, 'lowres_input', copy.deepcopy(lowres))
        
        # 2. Create magnified low-res image, and register high-res images with subsampled low-res ones
        f_magnify = config.lowres.magnify_factor
        logger.info('Magnify Dragonfly image with a factor of %.1f:', f_magnify)
        if skip_resize:
            hdu = fits.open('_lowres_{}.fits'.format(int(f_magnify)))
            lowres = Celestial(hdu[0].data, header=hdu[0].header)
        else:
            lowres.resize_image(f_magnify, method=config.fluxmodel.interp)
            lowres.save_to_fits('_lowres_{}.fits'.format(int(f_magnify)))

        logger.info('Register high resolution image "{0}" with "{1}"'.format(dir_hires_b, dir_lowres))
        if skip_resize:
            hdu = fits.open('_hires_b_reproj.fits')
            hires_b = Celestial(hdu[0].data, header=hdu[0].header)
            hdu.close()
            hdu = fits.open('_hires_r_reproj.fits')
            hires_r = Celestial(hdu[0].data, header=hdu[0].header)
            hdu.close()
        else:
            hdu = fits.open(dir_hires_b)
            if 'hsc' in dir_hires_b:
                array, _ = reproject_interp(hdu[1], lowres.header)
                # Note that reproject_interp does not conserve total flux
                # A factor is needed for correction.
                factor = (lowres.pixel_scale / (hdu[1].header['CD2_2'] * 3600))**2
                array *= factor
            else:
                array, _ = reproject_interp(hdu[0], lowres.header)
                factor = (lowres.pixel_scale / (hdu[0].header['CD2_2'] * 3600))**2
                array *= factor
            
            hires_b = Celestial(array, header=lowres.header)
            hires_b.save_to_fits('_hires_b_reproj.fits')
            hdu.close()
            
            logger.info('Register high resolution image "{0}" with "{1}"'.format(dir_hires_r, dir_lowres))
            hdu = fits.open(dir_hires_r)
            if 'hsc' in dir_hires_r:
                array, _ = reproject_interp(hdu[1], lowres.header)
                factor = (lowres.pixel_scale / (hdu[1].header['CD2_2'] * 3600))**2
                array *= factor
            else:
                array, _ = reproject_interp(hdu[0], lowres.header)
                factor = (lowres.pixel_scale / (hdu[0].header['CD2_2'] * 3600))**2
                array *= factor
            hires_r = Celestial(array, header=lowres.header)
            hires_r.save_to_fits('_hires_r_reproj.fits')
            hdu.close()

        # 3. Extract sources on hires images using SEP
        sigma = config.sex.sigma
        minarea = config.sex.minarea
        b = config.sex.b
        f = config.sex.f
        deblend_cont = config.sex.deblend_cont
        deblend_nthresh = config.sex.deblend_nthresh
        sky_subtract = config.sex.sky_subtract
        flux_aper = config.sex.flux_aper
        show_fig = config.sex.show_fig
            
        if skip_SE:
            hdu = fits.open('_hires_{}.fits'.format(int(f_magnify)))
            hires_3 = Celestial(hdu[0].data, header=hdu[0].header)
            hdu.close()
            hdu = fits.open('_colratio.fits')
            col_ratio = hdu[0].data
            hdu.close()
        else:
            logger.info('Build flux models on high-resolution images: Blue band')
            logger.info('    - sigma = %.1f, minarea = %d', sigma, minarea)
            logger.info('    - deblend_cont = %.5f, deblend_nthres = %.1f', deblend_cont, deblend_nthresh)
            _, _, b_imflux = Flux_Model(hires_b.image, hires_b.header, sigma=sigma, minarea=minarea, 
                                        deblend_cont=deblend_cont, deblend_nthresh=deblend_nthresh, 
                                        sky_subtract=sky_subtract, save=True, logger=logger)
            
            logger.info('Build flux models on high-resolution images: Red band')
            logger.info('    - sigma = %.1f, minarea = %d', sigma, minarea)
            logger.info('    - deblend_cont = %.5f, deblend_nthres = %.1f', deblend_cont, deblend_nthresh)
            _, _, r_imflux = Flux_Model(hires_r.image, hires_b.header, sigma=sigma, minarea=minarea, 
                                        deblend_cont=deblend_cont, deblend_nthresh=deblend_nthresh, 
                                        sky_subtract=sky_subtract, save=True, logger=logger)
            

            # 4. Make color correction, remove artifacts as well
            logger.info('Make color correction to blue band, remove artifacts as well')
            col_ratio = (b_imflux / r_imflux)
            col_ratio[np.isnan(col_ratio) | np.isinf(col_ratio)] = 0 # remove artifacts
            save_to_fits(col_ratio, '_colratio.fits', header=hires_b.header)
            
            color_term = config.lowres.color_term
            logger.info('    - color_term = {}'.format(color_term))
            median_col = np.nanmedian(col_ratio[col_ratio != 0])
            logger.info('    - median_color (blue/red) = {:.5f}'.format(median_col))

            fluxratio = col_ratio / median_col
            fluxratio[(fluxratio < 0.1) | (fluxratio > 10)] = 1 # remove extreme values
            col_correct = np.power(fluxratio, color_term)
            save_to_fits(col_correct, '_colcorrect.fits', header=hires_b.header)

            if config.lowres.band == 'r':
                hires_3 = Celestial(hires_r.image * col_correct, header=hires_r.header)
            elif config.lowres.band == 'g':
                hires_3 = Celestial(hires_b.image * col_correct, header=hires_b.header)
            else:
                raise ValueError('config.lowres.band must be "g" or "r"!')
            
            _ = hires_3.save_to_fits('_hires_{}.fits'.format(int(f_magnify)))
            
            setattr(results, 'hires_img', copy.deepcopy(hires_3))

            # Clear memory
            del r_imflux, b_imflux, hires_b, hires_r
            gc.collect()

        # 5. Extract sources on hi-res corrected image
        logger.info('Extract objects from color-corrected high resolution image with:')
        logger.info('    - sigma = %.1f, minarea = %d', sigma, minarea)
        logger.info('    - deblend_cont = %.5f, deblend_nthres = %.1f', deblend_cont, deblend_nthresh)
        objects, segmap = extract_obj(hires_3.image, b=b, f=f, sigma=sigma, minarea=minarea, 
                                      show_fig=False, flux_aper=flux_aper, sky_subtract=sky_subtract,
                                      deblend_nthresh=deblend_nthresh, 
                                      deblend_cont=deblend_cont, logger=logger)
        objects.write('_hires_obj_cat.fits', format='fits', overwrite=True)
        
        # 6. Remove bright stars and certain galaxies
        logger.info('Remove bright stars from this segmentation map, using SEP results.')
        logger.info('    - Bright star limit = {}'.format(config.starhalo.bright_lim))
        seg = copy.deepcopy(segmap)
        mag = config.hires.zeropoint - 2.5 * np.log10(abs(objects['flux']))
        objects.add_column(Column(data=mag, name='mag'))
        flag = np.where(mag < config.starhalo.bright_lim)
        for obj in objects[flag]:
            seg = seg_remove_obj(seg, obj['x'], obj['y'])
        objects[flag].write('_bright_stars_3.fits', format='fits', overwrite=True)
        logger.info('    - {} stars removed. '.format(len(flag[0])))
        if certain_gal_cat is not None:
            # Mask out certain galaxy here.
            logger.info('Remove objects from catalog "{}"'.format(certain_gal_cat))
            gal_cat = Table.read(certain_gal_cat, format='ascii')
            seg = mask_out_certain_galaxy(seg, hires_3.header, gal_cat=gal_cat, logger=logger)
        save_to_fits(seg, '_seg_3.fits', header=hires_3.header)
        
        setattr(results, 'segmap_nostar_nogal', seg)

        # 7. Remove artifacts from `hires_3` by color ratio and then smooth it
        # multiply by mask created from ratio of images - this removes all objects that are
        # only in g or r but not in both (artifacts, transients, etc)
        mask = seg * (col_ratio != 0)
        mask[mask != 0] = 1
        # Then blow mask up
        from astropy.convolution import Gaussian2DKernel, Box2DKernel, convolve
        smooth_radius = config.fluxmodel.gaussian_radius
        mask_conv = copy.deepcopy(mask)
        mask_conv[mask_conv > 0] = 1
        mask_conv = convolve(mask_conv.astype(float), Gaussian2DKernel(smooth_radius))
        seg_mask = (mask_conv >= config.fluxmodel.gaussian_threshold)

        hires_fluxmod = Celestial(seg_mask * hires_3.image, header=hires_3.header)
        hires_fluxmod.image[np.isnan(hires_fluxmod.image)] = 0
        _ = hires_fluxmod.save_to_fits('_hires_fluxmod.fits')
        logger.info('Flux model from high resolution image has been built.')
        setattr(results, 'hires_fluxmod', hires_fluxmod)
        
        # 8. Build kernel based on some stars
        img_hires = Celestial(hires_3.image.byteswap().newbyteorder(), 
                              header=hires_3.header, dataset=config.hires.dataset)
        img_lowres = Celestial(lowres.image.byteswap().newbyteorder(), 
                              header=lowres.header, dataset=config.hires.dataset)

        logger.info('Build convolving kernel to degrade high resolution image.')
        kernel_med, good_cat = Autokernel(img_hires, img_lowres, 
                                        int(f_magnify * config.kernel.kernel_size), 
                                        int(f_magnify * (config.kernel.kernel_size - config.kernel.kernel_edge)), 
                                        frac_maxflux=config.kernel.frac_maxflux, 
                                        show_figure=config.kernel.show_fig,
                                        nkernels=config.kernel.nkernel, logger=logger)
        # You can also circularize the kernel
        if config.kernel.circularize:
            logger.info('Circularize the kernel.')
            from compsub.utils import circularize
            kernel_med = circularize(kernel_med, n=14)
        save_to_fits(kernel_med, '_kernel_median.fits')
        setattr(results, 'kernel_med', kernel_med)

        # 9. Convolve this kernel to high-res image
        from astropy.convolution import convolve_fft
        logger.info('    - Convolving image, this could be a bit slow @_@')
        conv_model = convolve_fft(hires_fluxmod.image, kernel_med, boundary='fill', 
                            fill_value=0, nan_treatment='fill', normalize_kernel=False, allow_huge=True)
        save_to_fits(conv_model, '_lowres_model_{}.fits'.format(int(f_magnify)), header=hires_3.header)
        
        # Optionally remove low surface brightness objects from model: 
        if config.fluxmodel.unmask_lowsb:
            logger.info('    - Removing low-SB objects (SB > {}) from flux model.'.format(config.fluxmodel.sb_lim))
            from .utils import remove_lowsb
            hires_flxmd = remove_lowsb(hires_fluxmod.image, conv_model, kernel_med, seg, 
                                        "_hires_obj_cat.fits", 
                                        SB_lim=config.fluxmodel.sb_lim, 
                                        zeropoint=config.hires.zeropoint, 
                                        pixel_size=hires_fluxmod.pixel_scale, 
                                        unmask_ratio=config.fluxmodel.unmask_ratio, 
                                        minarea=config.fluxmodel.minarea * f_magnify**2,
                                        gaussian_radius=config.fluxmodel.gaussian_radius, 
                                        gaussian_threshold=config.fluxmodel.gaussian_threshold, 
                                        header=hires_fluxmod.header, 
                                        logger=logger)

            logger.info('    - Convolving image, this could be a bit slow @_@')
            conv_model = convolve_fft(hires_flxmd, kernel_med, boundary='fill', 
                                 fill_value=0, nan_treatment='fill', normalize_kernel=False, allow_huge=True)
            save_to_fits(conv_model, '_lowres_model_clean_{}.fits'.format(f_magnify), header=hires_3.header)
            setattr(results, 'hires_fluxmod', hires_flxmd)

        lowres_model = Celestial(conv_model, header=hires_3.header)
        res = Celestial(lowres.image - lowres_model.image, header=lowres.header)
        res.save_to_fits('_res_{}.fits'.format(f_magnify))

        lowres_model.resize_image(1 / f_magnify, method=config.fluxmodel.interp)
        lowres_model.save_to_fits('_lowres_model.fits')
        setattr(results, 'lowres_model_compact', copy.deepcopy(lowres_model))

        res.resize_image(1 / f_magnify, method=config.fluxmodel.interp)
        res.save_to_fits(output_name + '_res.fits')
        setattr(results, 'res', res)
        logger.info('Compact objects has been subtracted from low-resolution image! Saved as "{}".'.format(output_name + '_res.fits'))

        # 10. Subtract bright star halos! Only for those left out in flux model!
        star_cat = Table.read('_bright_stars_3.fits', format='fits')
        star_cat['x'] /= f_magnify
        star_cat['y'] /= f_magnify
        ra, dec = res.wcs.wcs_pix2world(star_cat['x'], star_cat['y'], 0)
        star_cat.add_columns([Column(data=ra, name='ra'), Column(data=dec, name='dec')])

        b = config.starhalo.b
        f = config.starhalo.f
        sigma = config.starhalo.sigma
        minarea = config.starhalo.minarea
        deblend_cont = config.starhalo.deblend_cont
        deblend_nthresh = config.starhalo.deblend_nthresh
        sky_subtract = config.starhalo.sky_subtract
        flux_aper = config.starhalo.flux_aper

        logger.info('Extract objects from compact-object-subtracted low-resolution image with:')
        logger.info('    - sigma = %.1f, minarea = %d', sigma, minarea)
        logger.info('    - deblend_cont = %.5f, deblend_nthres = %.1f', deblend_cont, deblend_nthresh)
        objects, segmap = extract_obj(res.image, 
                                    b=b, f=f, sigma=sigma, minarea=minarea,
                                    deblend_nthresh=deblend_nthresh, 
                                    deblend_cont=deblend_cont, 
                                    sky_subtract=sky_subtract, show_fig=False, 
                                    flux_aper=flux_aper, logger=logger)
        
        ra, dec = res.wcs.wcs_pix2world(objects['x'], objects['y'], 0)
        objects.add_columns([Column(data=ra, name='ra'), Column(data=dec, name='dec')])
        
        # Match two catalogs
        logger.info('Stack stars to get PSF model!')
        logger.info('    - Match detected objects with previously discard stars')
        temp, sep2d, _ = match_coordinates_sky(SkyCoord(ra=star_cat['ra'], dec=star_cat['dec'], unit='deg'),
                                               SkyCoord(ra=objects['ra'], dec=objects['dec'], unit='deg'))
        #temp = temp[sep2d < 5 * u.arcsec]
        bright_star_cat = objects[np.unique(temp)]
        mag = config.lowres.zeropoint - 2.5 * np.log10(bright_star_cat['flux'])
        bright_star_cat.add_column(Column(data=mag, name='mag'))
        
        if certain_gal_cat is not None:
            ## Remove objects in GAL_CAT
            temp, dist, _ = match_coordinates_sky(
                                SkyCoord(ra=gal_cat['ra'], dec=gal_cat['dec'], unit='deg'),
                                SkyCoord(ra=bright_star_cat['ra'], dec=bright_star_cat['dec'], unit='deg'))
            to_remove = []
            for i, obj in enumerate(dist):
                if obj < 10 * u.arcsec:
                    to_remove.append(temp[i])
            if len(to_remove) != 0:
                bright_star_cat.remove_rows(np.unique(to_remove))
        
        bright_star_cat.write('_bright_star_cat.fits', format='fits', overwrite=True)
        setattr(results, 'bright_star_cat', bright_star_cat)

        #### Select non-edge good stars to stack ###
        halosize = config.starhalo.halosize
        padsize = config.starhalo.padsize
        # FWHM selection
        psf_cat = bright_star_cat[bright_star_cat['fwhm_custom'] < config.starhalo.fwhm_lim]
        # Mag selection
        psf_cat = psf_cat[psf_cat['mag'] < config.starhalo.bright_lim]
        psf_cat = psf_cat[psf_cat['mag'] > 12.0] # Discard heavily saturated stars

        ny, nx = res.image.shape
        non_edge_flag = np.logical_and.reduce([(psf_cat['x'] > padsize), (psf_cat['x'] < nx - padsize), 
                                               (psf_cat['y'] > padsize), (psf_cat['y'] < ny - padsize)])
        psf_cat = psf_cat[non_edge_flag]                                        
        psf_cat.sort('flux')
        psf_cat.reverse()
        psf_cat = psf_cat[:int(config.starhalo.n_stack)]
        logger.info('    - Get {} stars to be stacked!'.format(len(psf_cat)))
        setattr(results, 'psf_cat', psf_cat)

        # Construct and stack `Stars`.
        size = 2 * halosize + 1
        stack_set = np.zeros((len(psf_cat), size, size))
        bad_indices = []
        for i, obj in enumerate(psf_cat):
            try:
                sstar = Star(results.lowres_input.image, header=results.lowres_input.header, starobj=obj, 
                             halosize=halosize, padsize=padsize)
                cval = config.starhalo.cval
                if isinstance(cval, str) and 'nan' in cval.lower():
                    cval = np.nan
                else:
                    cval = float(cval)

                sstar.centralize(method=config.starhalo.interp)
                
                if config.starhalo.mask_contam is True:
                    sstar.mask_out_contam(sigma=4.0, deblend_cont=0.0001, show_fig=False, verbose=False)
                    #sstar.image = sstar.get_masked_image(cval=cval)
                    #sstar.mask_out_contam(sigma=3, deblend_cont=0.0001, show_fig=False, verbose=False)
                #sstar.sub_bkg(verbose=False)
                if config.starhalo.norm == 'flux_ann':
                    stack_set[i, :, :] = sstar.get_masked_image(cval=cval) / sstar.fluxann
                else:
                    stack_set[i, :, :] = sstar.get_masked_image(cval=cval) / sstar.flux
                
            except Exception as e:
                stack_set[i, :, :] = np.ones((size, size)) * 1e9
                bad_indices.append(i)
                logger.info(e)
                print(e)

        from astropy.stats import sigma_clip
        stack_set = np.delete(stack_set, bad_indices, axis=0)
        median_psf = np.nanmedian(stack_set, axis=0)
        median_psf = psf_bkgsub(median_psf, int(config.starhalo.edgesize))
        median_psf = convolve(median_psf, Box2DKernel(1))
        sclip = sigma_clip(stack_set, axis=0, maxiters=3)
        sclip.data[sclip.mask] = np.nan
        error_psf = np.nanstd(sclip.data, ddof=2, axis=0) / np.sqrt(np.sum(~np.isnan(sclip.data), axis=0))
        save_to_fits(median_psf, '_median_psf.fits');
        save_to_fits(error_psf, '_error_psf.fits');
        
        setattr(results, 'PSF', median_psf)
        setattr(results, 'PSF_err', error_psf)
        
        logger.info('    - Stars are stacked successfully!')
        save_to_fits(stack_set, '_stack_bright_stars.fits')
        
        # 11. Build starhalo models and then subtract from "res" image
        if wide_psf:
            results = self._subtract_widePSF(results, res, halosize, bright_star_cat, median_psf, 
                            lowres_model, output_name, skip_mast=skip_mast, mast_catalog=mast_catalog)
        else:
            results = self._subtract_stackedPSF(results, res, halosize, bright_star_cat, median_psf, lowres_model, output_name)
        
        img_sub = results.lowres_final_unmask.image

        # 12. Mask out dirty things!
        if config.clean.clean_img:
            logger.info('Clean the image!')
            model_mask = convolve(1e3 * results.lowres_model.image / np.nansum(results.lowres_model.image),
                                  Gaussian2DKernel(config.clean.gaussian_radius))
            model_mask[model_mask < config.clean.gaussian_threshold] = 0
            model_mask[model_mask != 0] = 1
            # Mask out very bright stars, according to their radius
            totmask = bright_star_mask(model_mask.astype(bool), bright_star_cat, 
                                       bright_lim=config.clean.bright_lim, 
                                       r=config.clean.r)
            totmask = convolve(totmask.astype(float), Box2DKernel(2))
            totmask[totmask > 0] = 1
            if config.clean.replace_with_noise:
                logger.info('    - Replace artifacts with noise.')
                from mrf.utils import img_replace_with_noise
                final_image = img_replace_with_noise(img_sub.byteswap().newbyteorder(), totmask)
            else:
                logger.info('    - Replace artifacts with void.')
                final_image = img_sub * (~totmask.astype(bool))
            
            save_to_fits(final_image, output_name + '_final.fits', header=res.header)
            save_to_fits(totmask.astype(float), output_name + '_mask.fits', header=res.header)
            setattr(results, 'lowres_final', Celestial(final_image, header=res.header))
            setattr(results, 'lowres_mask', Celestial(totmask.astype(float), header=res.header))
            logger.info('The final result is saved as "{}"!'.format(output_name + '_final.fits'))
            logger.info('The mask is saved as "{}"!'.format(output_name + '_mask.fits'))
        # Delete temp files
        if config.clean.clean_file:
            logger.info('Delete all temporary files!')
            os.system('rm -rf _*.fits')

        # 13. determine detection depth
        from .sbcontrast import cal_sbcontrast
        _  = cal_sbcontrast(final_image, totmask.astype(int), 
                             config.lowres.pixel_scale, config.lowres.zeropoint, 
                             scale_arcsec=60, minfrac=0.8, minback=6, verbose=True, logger=logger);
        
        # Plot out the result
        plt.rcParams['text.usetex'] = False
        fig, [ax1, ax2, ax3] = plt.subplots(1, 3, figsize=(15, 8))
        hdu = fits.open(dir_lowres)
        lowres_image = hdu[0].data
        ax1 = display_single(lowres_image, ax=ax1, scale_bar_length=300, 
                            scale_bar_y_offset=0.3, pixel_scale=config.lowres.pixel_scale, 
                            add_text='Lowres', text_y_offset=0.7)
        ax2 = display_single(lowres_model.image, ax=ax2, scale_bar=False, 
                            add_text='Model', text_y_offset=0.7)
        ax3 = display_single(final_image, ax=ax3, scale_bar=False, 
                            add_text='Residual', text_y_offset=0.7)
        for ax in [ax1, ax2, ax3]:
            ax.axis('off')
        plt.subplots_adjust(wspace=0.02)
        plt.savefig(output_name + '_result.png', bbox_inches='tight', facecolor='silver')
        plt.close()
        logger.info('Task finished! (⁎⁍̴̛ᴗ⁍̴̛⁎)')

        return results

    def _subtract_stackedPSF(self, results, res, halosize, bright_star_cat, median_psf, lowres_model, output_name):
        from astropy.coordinates import SkyCoord, match_coordinates_sky
        import astropy.units as u
        from mrf.celestial import Celestial, Star
        from mrf.utils import save_to_fits

        config = self.config
        logger = self.logger

        # 11. Build starhalo models and then subtract from "res" image
        logger.info('Draw star halo models onto the image, and subtract them!')
        # Make an extra edge, move stars right
        ny, nx = res.image.shape
        im_padded = np.zeros((ny + 2 * halosize, nx + 2 * halosize))
        # Making the left edge empty
        im_padded[halosize: ny + halosize, halosize: nx + halosize] = res.image
        im_halos_padded = np.zeros_like(im_padded)

        for i, obj in enumerate(bright_star_cat):
            spsf = Celestial(median_psf, header=lowres_model.header)
            x = obj['x']
            y = obj['y']
            x_int = x.astype(np.int)
            y_int = y.astype(np.int)
            dx = -1.0 * (x - x_int)
            dy = -1.0 * (y - y_int)
            spsf.shift_image(-dx, -dy, method=config.starhalo.interp)
            x_int, y_int = x_int + halosize, y_int + halosize
            if config.starhalo.norm == 'flux_ann':
                im_halos_padded[y_int - halosize:y_int + halosize + 1, 
                                x_int - halosize:x_int + halosize + 1] += spsf.image * obj['flux_ann']
            else:
                im_halos_padded[y_int - halosize:y_int + halosize + 1, 
                                x_int - halosize:x_int + halosize + 1] += spsf.image * obj['flux']

        im_halos = im_halos_padded[halosize: ny + halosize, halosize: nx + halosize]
        setattr(results, 'lowres_model_star', Celestial(im_halos, header=lowres_model.header))
        img_sub = res.image - im_halos
        setattr(results, 'lowres_final_unmask', Celestial(img_sub, header=res.header))
        lowres_model.image += im_halos
        setattr(results, 'lowres_model', lowres_model)

        save_to_fits(im_halos, '_lowres_halos.fits', header=lowres_model.header)
        save_to_fits(img_sub, output_name + '_halosub.fits', 
                        header=lowres_model.header)
        save_to_fits(lowres_model.image, output_name + '_model_halos.fits', 
                        header=lowres_model.header)
        logger.info('Bright star halos are subtracted!')
        return results

    def _subtract_widePSF(self, results, res, halosize, bright_star_cat, median_psf, lowres_model, output_name, 
                          skip_mast=False, mast_catalog=None):
        from astropy.coordinates import SkyCoord, match_coordinates_sky
        import astropy.units as u
        from mrf.celestial import Celestial, Star
        from mrf.utils import save_to_fits, bright_star_mask
        from mrf.display import display_single
        from astropy.convolution import convolve, Box2DKernel, Gaussian2DKernel

        config = self.config
        logger = self.logger

        #### 10.5: Build hybrid PSF with Qing's modelling.
        from .utils import save_to_fits
        from .utils import compute_Rnorm
        from .modeling import PSF_Model
        from photutils import CircularAperture
        
        ### PSF Parameters
        psf_size = 501                 # in pixel
        pixel_scale = config.lowres.pixel_scale  # in arcsec/pixel
        frac = config.wide_psf.frac                          # fraction of power law component (from fitting stacked PSF)
        beta = config.wide_psf.beta                            # moffat beta, in arcsec. This parameter is not used here. 
        fwhm = config.wide_psf.fwhm * pixel_scale           # moffat fwhm, in arcsec. This parameter is not used here. 
        n_s = np.array(config.wide_psf.n_s)                          # power-law index
        theta_s = np.array(config.wide_psf.theta_s)      # transition radius in arcsec
        ### Construct model PSF
        params = {"fwhm": fwhm, "beta": beta, "frac": frac, "n_s": n_s, 'theta_s': theta_s}
        logger.info('Wide-PSF parameters:')
        logger.info('    - n=%r'%params['n_s'])
        logger.info('    - theta=%r'%params['theta_s'])
        psf = PSF_Model(params, aureole_model='multi-power')
        ### Build grid of image for drawing
        psf.pixelize(pixel_scale)
        ### Generate the aureole of PSF
        psf_e, _ = psf.generate_aureole(psf_range=2 * psf_size)
        ### Hybrid radius (in pixel)
        try:
            hybrid_r = config.starhalo.hybrid_r
        except:
            hybrid_r = 12

        ### Inner PSF: from stacking stars
        inner_psf = copy.deepcopy(median_psf)
        inner_psf /= np.sum(inner_psf) # Normalize
        inner_size = inner_psf.shape   
        inner_cen = [int(x / 2) for x in inner_size]
        ##### flux_inn is the flux inside an annulus, we use this to scale inner and outer parts
        flux_inn = compute_Rnorm(inner_psf, None, inner_cen, R=hybrid_r, display=False, mask_cross=False)[1]
        ##### We only remain the stacked PSF inside hybrid radius. 
        aper = CircularAperture(inner_cen, hybrid_r).to_mask()
        mask = aper.to_image(inner_size) == 0
        inner_psf[mask] = np.nan

        ### Make new empty PSF
        outer_cen = (int(psf_size / 2), int(psf_size / 2))
        new_psf = np.zeros((int(psf_size), int(psf_size)))
        new_psf[outer_cen[0] - inner_cen[0]:outer_cen[0] + inner_cen[0] + 1, 
                outer_cen[1] - inner_cen[1]:outer_cen[1] + inner_cen[1] + 1] = inner_psf

        ### Outer PSF: from model
        outer_psf = psf_e.drawImage(nx=psf_size, ny=psf_size, scale=config.lowres.pixel_scale, method="no_pixel").array
        outer_psf /= np.sum(outer_psf) # Normalize
        ##### flux_out is the flux inside an annulus, we use this to scale inner and outer parts
        flux_out = compute_Rnorm(outer_psf, None, outer_cen, 
                                R=hybrid_r, display=False, mask_cross=False)[1]

        ##### Scale factor: the flux ratio near hybrid radius 
        scale_factor = flux_out / flux_inn
        temp = copy.deepcopy(outer_psf)
        new_psf[np.isnan(new_psf)] = temp[np.isnan(new_psf)] / scale_factor # fill `nan`s with the outer PSF
        temp[outer_cen[0] - inner_cen[0]:outer_cen[0] + inner_cen[0] + 1, 
                outer_cen[1] - inner_cen[1]:outer_cen[1] + inner_cen[1] + 1] = 0
        new_psf += temp / scale_factor
        new_psf /= np.sum(new_psf) # Normalize
        factor = np.sum(median_psf) / np.sum(new_psf[outer_cen[0] - inner_cen[0]:outer_cen[0] + inner_cen[0] + 1, 
                                                    outer_cen[1] - inner_cen[1]:outer_cen[1] + inner_cen[1] + 1])
        new_psf *= factor
        save_to_fits(new_psf, './wide_psf.fits')
        setattr(results, 'wide_PSF', new_psf)


        ### 11. Build starhalo models and then subtract from "res" image
        logger.info('Draw star halo models onto the image, and subtract them!')
        if skip_mast:
            if mast_catalog is None:
                ps1_cat = Table.read('./_ps1_cat.fits')
            else:
                logger.info(f"Load Pan-STARRS catalog as {mast_catalog}")
                ps1_cat = Table.read(mast_catalog)
        else:
            ### Use Pan-STARRS catalog to normalize these bright stars
            from mrf.utils import ps1cone
            # Query PANSTARRS starts
            constraints = {'nDetections.gt':1, config.lowres.band + 'MeanPSFMag.lt':18}
            # strip blanks and weed out blank and commented-out values
            columns = """objID,raMean,decMean,raMeanErr,decMeanErr,nDetections,ng,nr,gMeanPSFMag,rMeanPSFMag""".split(',')
            columns = [x.strip() for x in columns]
            columns = [x for x in columns if x and not x.startswith('#')]
            logger.info('Retrieving Pan-STARRS catalog from MAST! Please wait!')
            # Try query MAST for a few times
            for attempt in range(3):
                try:
                    ps1result = ps1cone(results.lowres_input.ra_cen, results.lowres_input.dec_cen, results.lowres_input.diag_radius.to(u.deg).value, 
                                        release='dr2', columns=columns, verbose=False, **constraints)
                except HTTPError:
                    logger.info('Gateway Time-out. Will try Again.')
                else:
                    break
            else:
                sys.exit('504 Server Error: Failed Attempts. Exit.')
                
            ps1_cat = Table.read(ps1result, format='csv')
            ps1_cat.add_columns([Column(data = lowres_model.wcs.wcs_world2pix(ps1_cat['raMean'], ps1_cat['decMean'], 0)[0], 
                                        name='x_ps1'),
                                Column(data = lowres_model.wcs.wcs_world2pix(ps1_cat['raMean'], ps1_cat['decMean'], 0)[1], 
                                        name='y_ps1')])
            ps1_cat = ps1_cat[ps1_cat[config.lowres.band + 'MeanPSFMag'] != -999]
            ps1_cat.write('./_ps1_cat.fits', overwrite=True)

        ## Match PS1 catalog with SEP one
        temp, dist, _ = match_coordinates_sky(SkyCoord(ra=bright_star_cat['ra'], dec=bright_star_cat['dec'], unit='deg'),
                                            SkyCoord(ra=ps1_cat['raMean'], dec=ps1_cat['decMean'], unit='deg'))
        flag = dist < 5 * u.arcsec
        temp = temp[flag]
        reorder_cat = vstack([bright_star_cat[flag], bright_star_cat[~flag]], join_type='outer')
        bright_star_cat = hstack([reorder_cat, ps1_cat[temp]], join_type='outer')     
        bright_star_cat.write('_bright_star_cat.fits', format='fits', overwrite=True)
        setattr(results, 'bright_star_cat', bright_star_cat)

        ### Fit an empirical relation between PS1 magnitude and SEP flux
        from astropy.table import MaskedColumn
        if isinstance(bright_star_cat['rMeanPSFMag'], MaskedColumn):
            mask = (~bright_star_cat.mask[config.lowres.band + 'MeanPSFMag'])
            flag = (bright_star_cat[config.lowres.band + 'MeanPSFMag'] < 16) & (bright_star_cat[config.lowres.band + 'MeanPSFMag'] > 0) & mask
        else:
            flag = (bright_star_cat[config.lowres.band + 'MeanPSFMag'] < 16) & (bright_star_cat[config.lowres.band + 'MeanPSFMag'] > 0)
        x = bright_star_cat[flag][config.lowres.band + 'MeanPSFMag']
        y = -2.5 * np.log10(bright_star_cat[flag]['flux']) # or flux_ann
        pfit = np.polyfit(x, y, 2) # second-order polynomial
        plt.scatter(x, y, s=13)
        plt.plot(np.linspace(10, 16, 20), np.poly1d(pfit)(np.linspace(10, 16, 20)), color='red')
        plt.xlabel('MeanPSFMag')
        plt.ylabel('-2.5 Log(SE flux)')
        plt.savefig('./PS1-normalization.png')
        plt.close()

        # Make an extra edge, move stars right
        ny, nx = res.image.shape
        im_padded = np.zeros((int(ny + psf_size), int(nx + psf_size)))
        # Making the left edge empty
        im_padded[int(psf_size/2): ny + int(psf_size/2), int(psf_size/2): nx + int(psf_size/2)] = res.image
        im_halos_padded = np.zeros_like(im_padded)

        # Stack stars onto the canvas
        for i, obj in enumerate(bright_star_cat):
            spsf = Celestial(new_psf, header=lowres_model.header)
            x = obj['x']
            y = obj['y']
            x_int = x.astype(np.int)
            y_int = y.astype(np.int)
            dx = -1.0 * (x - x_int)
            dy = -1.0 * (y - y_int)
            spsf.shift_image(-dx, -dy, method=config.starhalo.interp)
            x_int, y_int = x_int + int(psf_size/2), y_int + int(psf_size/2)

            if obj['mag'] < 15.5:
                if obj[config.lowres.band + 'MeanPSFMag']:
                    norm = 10**((-np.poly1d(pfit)(obj[config.lowres.band + 'MeanPSFMag'])) / 2.5)
                else:
                    norm = obj['flux']
            else:
                norm = obj['flux']

            im_halos_padded[y_int - int(psf_size/2):y_int + int(psf_size/2) + 1, 
                            x_int - int(psf_size/2):x_int + int(psf_size/2) + 1] += spsf.image * norm

        im_halos = im_halos_padded[int(psf_size/2): ny + int(psf_size/2), int(psf_size/2): nx + int(psf_size/2)]

        model_star = Celestial(im_halos, header=lowres_model.header)

        model_star.shift_image(-1/config.lowres.magnify_factor, -1/config.lowres.magnify_factor, method=config.starhalo.interp)

        setattr(results, 'lowres_model_star', model_star)
        img_sub = res.image - model_star.image
        setattr(results, 'lowres_final_unmask', Celestial(img_sub, header=res.header))
        lowres_model.image += model_star.image
        setattr(results, 'lowres_model', lowres_model)

        save_to_fits(model_star.image, '_lowres_halos.fits', header=lowres_model.header)
        save_to_fits(img_sub, output_name + '_halosub.fits', 
                        header=lowres_model.header)
        save_to_fits(lowres_model.image, output_name + '_model_halos.fits', 
                        header=lowres_model.header)
        logger.info('Bright star halos are subtracted!')
        return results


class MrfTileMode():
    '''
    MRF “tile mode” task class. This class implements `mrf` in a "tile mode", with wide-angle PSF incorporated.
    '''
    def __init__(self, config_dict):
        """
        Initialize ``MrfTileMode`` class. 

        Parameters:
            config_dict (dict): a configuration dictionary.
                Example dict is like:
                ```
                tile_params = {
                    "target_name": 'N1052',
                    "band": "g",
                    "ra": 40.2754339,
                    "dec": -8.2716477,
                    "cutout_size": 5500,  # arcsec
                    "low_res_pix_scale": 2.5,  # arcsec/pix
                    "high_res_pix_scale": 0.262, # arcsec/pix
                    "zerop_g": 27.34875211073704,
                    "zerop_r": 27.06140642787075,
                    "high_res_source": 'decals',
                    "mrf_task_file": 'config-N1052-g.yaml',
                    "low_res_path": './Images/coadd_SloanG_NGC_1052_pcp_pcr.fits',
                    "high_res_path_g": './Images/DECaLS_g.fits',
                    "high_res_path_r": './Images/DECaLS_r.fits'
                }
                ```
        Returns:
            None
        """
        # Import configuration dictionary
        config = Config(config_dict)
        self.config_dict = config_dict
        self.config = config
        
    def set_logger(self, output_name='mrf', verbose=True):
        """
        Set logger for ``MrfTileMode``. 
        The logger will record the time and each output. 
        The log file will be saved locally.

        Parameters:
            verbose (bool): If False, the logger will be silent. 

        Returns:
            logger (``logging.logger`` object)
        """
        import logging
        import sys
        if verbose:
            log_filename = output_name + '.log'
            logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO, 
                                handlers=[logging.StreamHandler(sys.stdout),
                                          logging.FileHandler(log_filename, mode='w')])
            self.logger = logging.getLogger(log_filename)                          
        else:
            logger = logging.getLogger('mylogger')
            logger.propagate = False
            self.logger = logger
        return self.logger 
    
    def run(self, max_size=8000, overlap=0.05, tile_dir='./Images/tile', 
            skip_mast=False, mast_catalog=f'./_ps1_cat.fits', skip_cut_tile=False, skip_rebin=False, skip_mrf=False, 
            skip_trim=False, stitch_method='swarp', show_panstarrs=False, verbose=True):
        """
        Run MRF task in "tile mode". 
        Written by Colleen Gilhuly (U.Toronto) and Jiaxuan Li.

        Parameters:
            max_size (int): the lower threshold of activating "tile mode", in the frame of high-res image.
                If the size of high-res image is larger than this ``max_size``, "tile mode" will be activated.
            overlap (float): the fraction of overlaps among tiles.
            tile_dir (str): the directory of storing tile images and files.
            skip_mast (bool): whether skip downloading PAN-STARRS catalog from MAST server. 
                If True, the code will use the existing catalog under the current directory. 
                This is designed for the case when you need to tweak parameters and save some time.
            skip_cut_tile (bool): whether skip cropping the images to the designated (ra, dec, cutout_size). 
                This is designed for the case when you need to tweak parameters and save some time.
            skip_rebin (bool): whether skip rebinning the high-resolution image and convolving that with 1 pix Gaussian kernel.
                This is designed for the case when you need to tweak parameters and save some time.
            skip_mrf (bool): whether skip implementing MRF on each tile.
                This is designed for the case when you need to tweak parameters and save some time.
            skip_trim (bool): whether skip trim MRF-ed tiles. 
                This is designed for the case when you need to tweak parameters and save some time.
            stitch_method (string): method used to stitch tiles together. Options are "swarp" and "reproject".
                If you use "swarp", you need to have `SWarp` installed on your computer. 
                See https://www.astromatic.net/software/swarp for help.
            show_panstarrs (bool): whether display PAN-STARRS bright star catalog on the image.
            verbose (bool): If True, it will make a log file recording the process. 
            
        Returns:
            None
        """
        
        # Import all the important stuff
        import numpy as np
        from astropy.io import fits
        import matplotlib.pyplot as plt

        # MRF
        from mrf import sbcontrast
        from mrf import download
        from mrf.task import MrfTask
        from mrf.celestial import Celestial
        from mrf.display import display_single, SEG_CMAP
        from mrf.utils import img_cutout

        # astropy
        from astropy.convolution import convolve, Gaussian2DKernel
        from astropy.nddata import Cutout2D
        from astropy import wcs #import WCS, utils
        from astropy import units as u
        from astropy.table import Table

        import timeit
        import os
    
        config = self.config
        logger = self.set_logger(output_name=config.target_name, verbose=verbose)
        results = Results(config)
        
        target_name = config.target_name
        band = config.band
        ra, dec = config.ra, config.dec
        cutout_size = config.cutout_size
        low_res_pix_scale = config.low_res_pix_scale
        high_res_pix_scale = config.high_res_pix_scale
        zerop_g, zerop_r = config.zerop_g, config.zerop_r
        
        high_res_source = config.high_res_source
        mrf_task_file = config.mrf_task_file
        low_res_path = config.low_res_path
        high_res_path = {'g': config.high_res_path_g, 
                         'r': config.high_res_path_r}
        high_res_path_g = high_res_path['g']
        high_res_path_r = high_res_path['r']
        
        logger.info(f'Running "Tile Mode" Multi-Resolution Filtering for object {target_name}!')
        
        ##### Build directories #####
        if not os.path.isdir('./Images/'):
            os.mkdir('./Images/')
            
        if not os.path.isdir('./Images/tile/'):
            os.mkdir('./Images/tile/')
        
        ##### Crop images to the "cutout_size", centered at "ra" and "dec" #####
        logger.info(f'Crop images to {cutout_size} x {cutout_size} arcsec, centered at RA = {ra:.2f} and Dec = {dec:.2f}')
        #--- low res ---#
        hdu = fits.open(low_res_path)
        img = hdu[0].data
        hdr = hdu[0].header
        img_cutout(img, wcs.WCS(hdr), ra, dec, size=cutout_size, 
                   pixel_scale=low_res_pix_scale, img_header=hdr, 
                   prefix=f'./Images/{target_name}-df-{band}');
        hdu.close()
        
        #--- high res ---#
        for filt in ['g', 'r']:
            hdu = fits.open(high_res_path[filt])
            img = hdu[0].data
            hdr = hdu[0].header
            
            img_cutout(img, wcs.WCS(hdr), ra, dec, size=cutout_size, 
                       pixel_scale=high_res_pix_scale, 
                       img_header=hdr, 
                       prefix=f'./Images/{target_name}-{high_res_source}-{filt}');
            hdu.close()
        
        ##### Download PAN-STARRS catalog #####
        hdu = fits.open(f'./Images/{target_name}-df-{band}.fits')
        img = hdu[0].data
        hdr = hdu[0].header
        w = wcs.WCS(hdr)

        if not skip_mast:
            logger.info(f'Download PAN-STARRS catalog from MAST. This takes some time, and depends on your internet condition.')
            #-- Download Pan-STARRS catalog --#
            from mrf.utils import ps1cone
            from astropy.table import Table, Column
            constraints = {'nDetections.gt':1, band + 'MeanPSFMag.lt':18}
            # strip blanks and weed out blank and commented-out values
            columns = """objID,raMean,decMean,raMeanErr,decMeanErr,nDetections,ng,nr,gMeanPSFMag,rMeanPSFMag""".split(',')
            columns = [x.strip() for x in columns]
            columns = [x for x in columns if x and not x.startswith('#')]

            ps1result = ps1cone(ra, dec, (0.5 * np.sqrt(2) * cutout_size / 3600), 
                                release='dr2', columns=columns, verbose=False, **constraints)
            ps1_cat = Table.read(ps1result, format='csv')
            ps1_cat.add_columns([Column(data = w.wcs_world2pix(ps1_cat['raMean'], ps1_cat['decMean'], 0)[0], 
                                        name='x_ps1'),
                                 Column(data = w.wcs_world2pix(ps1_cat['raMean'], ps1_cat['decMean'], 0)[1], 
                                        name='y_ps1')])
            ps1_cat = ps1_cat[ps1_cat[band + 'MeanPSFMag'] != -999]
            ps1_cat.write(f'./PS1-{target_name}-field.fits', overwrite=True)
        else:
            assert os.path.isfile(f'./PS1-{target_name}-field.fits'), f"You don't have PAN-STARRS catalog saved as './PS1-{name}-field.fits'!"
            logger.info(f"Load PAN-STARRS catalog from './PS1-{target_name}-field.fits'")
            ps1_cat = Table.read(f'./PS1-{target_name}-field.fits')
        
        if show_panstarrs:
            from mrf.display import draw_circles
            draw_circles(img, ps1_cat, colnames=['x_ps1', 'y_ps1'], pixel_scale=low_res_pix_scale)
        
        ##### Assess size of high-res counterpart to input image #####
        # Arbitrarily examining g-band size first 
        # (assuming both bands are available and similar in extent)
        highres_header = fits.getheader(high_res_path_g)
        highres_x = highres_header['NAXIS1']
        highres_y = highres_header['NAXIS2']
        
        # If either dimension is substantially larger than the maximum size, 
        # it will be worthwhile to proceed in tile mode
        if highres_x >= 1.5 * max_size or highres_y >= 1.5 * max_size:
            tilemode = True
            logger.info( f"Tile mode MRF recommended for high-res image of dimensions {highres_x} x {highres_y}" )

            # Need to compare sizes of high res g- and r-band files!
            # Can run into some trouble with mismatched tiles if full images differ in size 
            #!# Could also have problems if centers are significantly misaligned #!#

            highres_header_r = fits.getheader(high_res_path_r)  
            highres_x_r = highres_header_r['NAXIS1']
            highres_y_r = highres_header_r['NAXIS2']  
            
            # The smaller image should be used to define the tiles
            # Figuring out here which high res image is smaller:

            if highres_x_r == highres_x and highres_y_r == highres_y:
                logger.info( "--> High resolution g- and r-band images match in size.")
                master_band = 'g'

            elif highres_x_r < highres_x and highres_y_r < highres_y:
                logger.info( "--> High resolution r-band image smaller than g-band.")
                master_band = 'r'
                highres_x = highres_x_r
                highres_y = highres_y_r

            elif highres_x_r > highres_x and highres_y_r > highres_y:
                logger.info( "--> High resolution g-band image smaller than r-band.")
                master_band = 'g'

            elif highres_x_r * highres_y_r < highres_x * highres_y:
                logger.info( "--> High resolution r-band image *area* smaller than g-band.")
                master_band = 'r'
                highres_x = highres_x_r
                highres_y = highres_y_r

            elif highres_x_r * highres_y_r > highres_x * highres_y:
                logger.info( "--> High resolution g-band image *area* smaller than r-band.")
                master_band = 'g'

            # Would be surprised to see this happen, unless it's a case of x * y = y * x 
            # This wouldn't be a good sign for having good coverage in both bands
            else:
                logger.info( "--> How unusual, high resolution images are different in dimensions but equal in area!")
                master_band = 'g'
        else:
            tilemode = False
            assert tilemode, "Tile mode MRF not required. The size of high-res image is smaller than 1.5 * max_size."
            return 
        
        
        ##### Calculate number of tiles along each axis #####
        logger.info('Defining number of tiles and their extent on high-res image')
        from astropy.nddata.utils import NoOverlapError

        # Determining number of tiles along each axis
        # Overlap is important for avoiding edge effects or missing effect of bright stars outside tile
        N_x = np.ceil( (1. + 2.*overlap) * highres_x/max_size )
        N_y = np.ceil( (1. + 2.*overlap) * highres_y/max_size )  
        N_tiles = int(N_x * N_y)

        logger.info( f"--> Number of tiles: {N_x} x {N_y} = {N_tiles}")

        len_x = np.ceil( highres_x / N_x )
        len_y = np.ceil( highres_y / N_y )

        logger.info( f"--> Size of tiles: {len_x} x {len_y} pixels (in high-res pixel scale)")    

        # Required to make/organize tiles:
        # - coordinates in tile grid ([0,0], [0,1], ...)
        # - coordinates of corners on full high-res image (xmin, xmax, ymin, ymax)
        # - some kind of name/handle/index to ease iteration over tiles

        tiles_xcen = []
        tiles_ycen = []
        tiles_xlen = []
        tiles_ylen = []

        # Loading in images
        hdu_g = fits.open(high_res_path_g)[0]
        wcs_g = wcs.WCS(hdu_g.header)     
        hdu_r = fits.open(high_res_path_r)[0]
        wcs_r = wcs.WCS(hdu_r.header) 

        hdu_lowres = fits.open(low_res_path)[0]
        wcs_lowres = wcs.WCS(hdu_lowres.header)

        j = 0
        skipped = 0

        for i in range( 0, N_tiles ):

            # Indices for keeping track of coordinates in tile grid
            # This won't be important later when corners are defined for each tile
            x_index = int( i % N_x );
            y_index = int( i / N_x );

            xmin = x_index * len_x
            ymin = y_index * len_y
            xmax = xmin + len_x
            ymax = ymin + len_y

            xmax = min( highres_x - 1, xmax )
            ymax = min( highres_y - 1, ymax )

            x_center = 0.5 * ( xmin + xmax )
            y_center = 0.5 * ( ymin + ymax )
            x_size = xmax - xmin
            y_size = ymax - ymin 

            # Now introducing overlap
            xmin = max( 0, xmin - int( overlap * len_x ) )
            ymin = max( 0, ymin - int( overlap * len_y ) )
            xmax = min( highres_x - 1, xmax + int( overlap * len_x ) )
            ymax = min( highres_y - 1, ymax + int( overlap * len_y ) )

            x_center = 0.5 * ( xmin + xmax )
            y_center = 0.5 * ( ymin + ymax )
            x_size = xmax - xmin
            y_size = ymax - ymin 

            if master_band == 'r':
                center_coords = wcs.utils.pixel_to_skycoord( x_center,
                                                             y_center,
                                                             wcs_r )
            else:
                center_coords = wcs.utils.pixel_to_skycoord( x_center,
                                                             y_center,
                                                             wcs_g )

            # Assuming same pixel scale for both high res images, so it doesn't matter which is used
            #!# Being lazy here and assuming pixel grid is not rotated w.r.t. RA and Dec.
            pix_scale_hires = np.abs( hdu_g.header["CD1_1"] ) * u.degree
            x_size *= pix_scale_hires
            y_size *= pix_scale_hires
            
            if not skip_cut_tile:
                try:
                    new_cutout_lowres = Cutout2D( hdu_lowres.data, 
                                                  position = center_coords, 
                                                  size = (y_size, x_size), 
                                                  wcs = wcs_lowres,
                                                  mode = 'partial',
                                                  copy = True )  

                    tile_lowres = fits.PrimaryHDU( new_cutout_lowres.data, hdu_lowres.header )
                    tile_lowres.header.update( new_cutout_lowres.wcs.to_header() )
                    for i in tile_lowres.header['PC*'].keys():
                        del tile_lowres.header[i]
                    tile_lowres.writeto(os.path.join(tile_dir, 
                                                     f"{target_name}-lowres-{band}-tile-{j}.fits"), 
                                        overwrite = True)

                except NoOverlapError:

                    skipped += 1
                    continue


                # Make the tile cutouts!
                new_cutout_g = Cutout2D( hdu_g.data, 
                                         position = center_coords, 
                                         size = (y_size, x_size), 
                                         wcs = wcs_g,
                                         mode = 'partial',
                                         copy = True )

                new_cutout_r = Cutout2D( hdu_r.data, 
                                         position = center_coords, 
                                         size = (y_size, x_size), 
                                         wcs = wcs_r,
                                         mode = 'partial',
                                         copy = True )        

                tile_g = fits.PrimaryHDU( new_cutout_g.data, hdu_g.header )
                tile_g.header.update( new_cutout_g.wcs.to_header() )
                for i in tile_g.header['PC*'].keys():
                    del tile_g.header[i]
                tile_g.writeto(os.path.join(tile_dir, 
                                            f"{target_name}-{high_res_source}-g-tile-{j}.fits"), 
                               overwrite = True)

                tile_r = fits.PrimaryHDU( new_cutout_r.data, hdu_r.header )
                tile_r.header.update( new_cutout_r.wcs.to_header() )
                for i in tile_r.header['PC*'].keys():
                    del tile_r.header[i]
                tile_r.writeto(os.path.join(tile_dir, 
                                            f"{target_name}-{high_res_source}-r-tile-{j}.fits"), 
                               overwrite = True)

            # Storing size and center of tiles before overlap
            # (Will be needed later when stitching results together)
            tiles_xcen.append( x_center )
            tiles_ycen.append( y_center )
            tiles_xlen.append( x_size )
            tiles_ylen.append( y_size )

            j += 1

        logger.info( f"Created {j} out of a possible {N_tiles} tiles.")
        
        
        if not skip_rebin:
            ##### Binning high-res images 2x2 and smoothing with Gaussian kernel #####
            logger.info('Binning high-res images 2x2 and smoothing with Gaussian kernel (radius = 1 pix)')
            # Do the above things for all N tiles
            for i in range( 0, N_tiles - skipped ):

                # Running timer in case this is slow x__x
                start_time = timeit.default_timer()

                hdu = fits.open( os.path.join(tile_dir, f"{target_name}-{high_res_source}-g-tile-{i}.fits") )
                hires_g = Celestial( hdu[0].data, header=hdu[0].header )
                hdu.close()

                hires_g.resize_image(0.5, method='iraf')
                hires_g.image = convolve( hires_g.image, Gaussian2DKernel(1) )
                hires_g.save_to_fits( os.path.join(tile_dir, f"{target_name}-{high_res_source}-binned-g-tile-{i}.fits") )

                hdu = fits.open( os.path.join(tile_dir, f"{target_name}-{high_res_source}-r-tile-{i}.fits") )
                hires_r = Celestial( hdu[0].data, header=hdu[0].header )
                hdu.close()

                hires_r.resize_image(0.5, method='iraf')
                hires_r.image = convolve( hires_r.image, Gaussian2DKernel(1) )
                hires_r.save_to_fits( os.path.join(tile_dir, f"{target_name}-{high_res_source}-binned-r-tile-{i}.fits") )

                elapsed = timeit.default_timer() - start_time
                logger.info( f"--> Binned and smoothed g- and r-band tile pair {i+1}/{N_tiles -skipped} in {elapsed:.2f} seconds" )

        
        if not skip_mrf:
            ##### Define and run MRF task #####
            logger.info( f"Run MRF for each tile" )
            from galsim.errors import GalSimValueError
            from urllib.error import HTTPError
            bad_tiles = []

            for i in range( 0, N_tiles - skipped ):
                task = MrfTask( mrf_task_file ) ## Will this need to be tweaked for tiles?
                try:
                    start_time = timeit.default_timer() ## Timing the task
                    results = task.run(os.path.join(tile_dir, f"{target_name}-lowres-{band}-tile-{i}.fits"),
                                       os.path.join(tile_dir, f"{target_name}-{high_res_source}-binned-g-tile-{i}.fits"),
                                       os.path.join(tile_dir, f"{target_name}-{high_res_source}-binned-r-tile-{i}.fits"), 
                                       "gal_cat.txt", 
                                       output_name=os.path.join(tile_dir, f"{target_name}-{band}-tile-{i}"), 
                                       wide_psf=True,
                                       verbose=False, 
                                       skip_mast=True, 
                                       mast_catalog=f'./PS1-{target_name}-field.fits')
                    elapsed = timeit.default_timer() - start_time
                    logger.info( f"--> MRF finished for tile {i+1}/{N_tiles} in {elapsed:.2f} seconds" )

                except GalSimValueError:
                    bad_tiles.append(i)
                    logger.info( ">>>>>>>>>>>>>>>>>>>> Skipping tile because flux model is empty!")
                    continue

                except AttributeError:
                    bad_tiles.append(i)
                    logger.info( ">>>>>>>>>>>>>>>>>>>> Skipping tile because probably ran out of good stars!")
                    continue

                except HTTPError:
                    bad_tiles.append(i)
                    logger.info( ">>>>>>>>>>>>>>>>>>>> Timeout while retrieving PanSTARRS catalog =(")
                    continue

        
        ##### Trim images and masks #####
        logger.info("Trim MRF-ed images and masks down to approximate extent of each tile")
        # If operating in tile mode, need to finally stitch tiles back together
        # Need to trim MRF-ed image down to approximate extent of tile
        if not skip_trim:
            # For each tile:
            for i in range( 0, N_tiles ):

                logger.info( f"--> Trimming source-subtracted tile {i+1}/{N_tiles} ...")

                # Grabbing high res WCS for reference (all tile coords and sizes are in this system)
                # Remember to use correct reference band (in case high res images are different sizes)
                if master_band == 'r':
                    hdu_highres = fits.open(high_res_path_r)[0]
                    wcs_highres = wcs.WCS( hdu_highres.header )

                else:
                    hdu_highres = fits.open(high_res_path_g)[0]
                    wcs_highres = wcs.WCS( hdu_highres.header )

                # Convert position and extent of tile to angular units

                center_coords = wcs.utils.pixel_to_skycoord( tiles_xcen[i],
                                                             tiles_ycen[i],
                                                             wcs_highres )
                # This entry should have been put in when making tiles
                x_size = tiles_xlen[i]
                y_size = tiles_ylen[i]

                # Make a Cutout2D on MRF-ed **image** using coordinates and angular size
                hdu = fits.open( os.path.join(tile_dir, f'{target_name}-{band}-tile-{i}_halosub.fits') )[0] 
                w = wcs.WCS(hdu.header) 

                try:
                    new_cutout = Cutout2D( hdu.data, 
                                           position = center_coords, 
                                           size = (y_size, x_size), 
                                           wcs = w,
                                           mode = 'partial',
                                           copy = True )        

                    tile_g = fits.PrimaryHDU( new_cutout.data, hdu.header )
                    tile_g.header.update( new_cutout.wcs.to_header() )
                    tile_g.header.pop('PC*')
                    tile_g.writeto(os.path.join(tile_dir, f'{target_name}_{band}_final_tile_{i}.fits'), 
                                   overwrite = True)
                except NoOverlapError:
                    logger.info( f"--> Tile {i+1} lies fully outside input low-res image, skipping!")
                    continue

                # Make a Cutout2D on MRF-ed **mask** using coordinates and angular size
                hdu = fits.open( os.path.join(tile_dir, f'{target_name}-{band}-tile-{i}_mask.fits') )[0] 
                w = wcs.WCS(hdu.header) 

                try:
                    new_cutout = Cutout2D( hdu.data, 
                                           position = center_coords, 
                                           size = (y_size, x_size), 
                                           wcs = w,
                                           mode = 'partial',
                                           copy = True )        

                    tile_g = fits.PrimaryHDU( new_cutout.data, hdu.header )
                    tile_g.header.update( new_cutout.wcs.to_header() )
                    tile_g.header.pop('PC*')
                    tile_g.writeto(os.path.join(tile_dir, f'{target_name}_{band}_final_tile_{i}_mask.fits'), 
                                   overwrite = True)
                except NoOverlapError:
                    logger.info( f"--> Tile {i+1} lies fully outside input low-res image, skipping!")
                    continue

            # Done looping over tiles

        
        ##### Stitch images #####
        if not (stitch_method == "swarp" or stitch_method == "reproject"):
            raise ValueError('"stitch_method" must be "swarp" or "reproject"! ')
        
        output_dir = tile_dir
        
        output_name = f'{target_name}-stitch'
        filename_list = [os.path.join(tile_dir, f'{target_name}_{band}_final_tile_{i}.fits') for i in range(16)]
        self._stitch(stitch_method, 'image', config, config.cutout_size, 
                     filename_list, output_dir, output_name, logger=logger)
        
        output_name = f'{target_name}-stitch-mask'
        filename_list = [os.path.join(tile_dir, f'{target_name}_{band}_final_tile_{i}_mask.fits') for i in range(16)]
        self._stitch(stitch_method, 'mask', config, config.cutout_size, 
                     filename_list, output_dir, output_name, logger=logger)
        
        
        ##### Combine mask with image #####
        img = fits.open(os.path.join(output_dir, f'{target_name}-stitch_{band}.fits'))[0].data
        hdr = fits.getheader(os.path.join(output_dir, f'{target_name}-stitch_{band}.fits'))
        msk = fits.open(os.path.join(output_dir, f'{target_name}-stitch-mask_{band}.fits'))[0].data.astype(bool)
        
        from mrf.celestial import Celestial
        final = Celestial(img * (~msk), header=hdr)
        final.save_to_fits(os.path.join(output_dir, f'{target_name}-final-{band}.fits'))
        
        logger.info(f"MRF tile mode finished! The final image is saved as " + os.path.join(output_dir, f'{target_name}-final.fits'))
        

    def _stitch(self, method, imgtype, config, imgsize,  
                filename_list, output_dir, output_name, logger=None):
        """
        Stitch tiles (images and masks) together.
        Written by Jiaxuan Li.

        Parameters:
            method (str): method of stitching. Options are "swarp" and "reproject".
            imgtype (str): "image" or "mask". This affects the combining type during "Swarp" and "reproject".
            config (Config object): configuration object.
            imgsize (int): size of the stitched image, in arcsec.
            filename_list (list of str): list of files which are going to be stitched together.
            output_dir (str): directory of output
            output_name (str): name of output file
        
        Returns:
            None
        """
        from astropy.io import fits
        
        band = config.band
        ra, dec = config.ra, config.dec
        low_res_pix_scale = config.low_res_pix_scale
        high_res_pix_scale = config.high_res_pix_scale
        
        imgsize /= 2.5
        imgsize = int(imgsize)
        
        if logger is not None:
            logger.info(f'Stitching MRF-ed {imgtype} tiles together using {method}...')

        if method.lower() == 'swarp':
            if imgtype == 'mask':
                combine_type = "SUM"
                resample = 'N'
            else:
                combine_type = "MEDIAN"
                resample = 'Y'
                
            # Configure ``swarp``
            with open("config_swarp.sh","w+") as f:
                # check if swarp is installed
                f.write('for cmd in swarp; do\n')
                f.write('\t hasCmd=$(which ${cmd} 2>/dev/null)\n')
                f.write('\t if [[ -z "${hasCmd}" ]]; then\n')
                f.write('\t\t echo "This script requires ${cmd}, which is not in your \$PATH." \n')
                f.write('\t\t exit 1 \n')
                f.write('\t fi \n done \n\n')

                # Write ``default.swarp``.
                # Combine-type = MEDIAN, Resample = Yes
                f.write('/bin/rm -f default.swarp \n')
                f.write('cat > default.swarp <<EOT \n')
                f.write('IMAGEOUT_NAME \t\t {}.fits      # Output filename\n'.format(os.path.join(output_dir, '_'.join([output_name, band]))))
                f.write('WEIGHTOUT_NAME \t\t {}_weights.fits     # Output weight-map filename\n\n'.format(os.path.join(output_dir, '_'.join([output_name, band]))))
                f.write('HEADER_ONLY            N               # Only a header as an output file (Y/N)?\nHEADER_SUFFIX          .head           # Filename extension for additional headers\n\n')
                f.write('#------------------------------- Input Weights --------------------------------\n\nWEIGHT_TYPE            NONE            # BACKGROUND,MAP_RMS,MAP_VARIANCE\n                                       # or MAP_WEIGHT\nWEIGHT_SUFFIX          weight.fits     # Suffix to use for weight-maps\nWEIGHT_IMAGE                           # Weightmap filename if suffix not used\n                                       # (all or for each weight-map)\n\n')
                f.write('#------------------------------- Co-addition ----------------------------------\n\nCOMBINE                Y               # Combine resampled images (Y/N)?\n')
                f.write('COMBINE_TYPE           {}          # MEDIAN,AVERAGE,MIN,MAX,WEIGHTED,CHI2\n                                       # or SUM\n\n'.format(combine_type))
                f.write('#-------------------------------- Astrometry ----------------------------------\n\nCELESTIAL_TYPE         NATIVE          # NATIVE, PIXEL, EQUATORIAL,\n                                       # GALACTIC,ECLIPTIC, or SUPERGALACTIC\nPROJECTION_TYPE        TAN             # Any WCS projection code or NONE\nPROJECTION_ERR         0.001           # Maximum projection error (in output\n                                       # pixels), or 0 for no approximation\nCENTER_TYPE            MANUAL          # MANUAL, ALL or MOST\n')
                f.write('CENTER   {0}, {1} # Image Center\n'.format(ra, dec))
                f.write('PIXELSCALE_TYPE        MANUAL          # MANUAL,FIT,MIN,MAX or MEDIAN\n')
                f.write('PIXEL_SCALE            {}  # Pixel scale\n'.format(low_res_pix_scale))
                f.write('IMAGE_SIZE             {0},{1} # scale = {2} arcsec/pixel\n\n'.format(imgsize, imgsize, high_res_pix_scale))
                f.write('#-------------------------------- Resampling ----------------------------------\n\nRESAMPLE               {}               # Resample input images (Y/N)?\n'.format(resample))
                f.write('RESAMPLE_DIR           .               # Directory path for resampled images\nRESAMPLE_SUFFIX        .resamp.fits    # filename extension for resampled images\n\nRESAMPLING_TYPE        LANCZOS3        # NEAREST,BILINEAR,LANCZOS2,LANCZOS3\n                                       # or LANCZOS4 (1 per axis)\nOVERSAMPLING           0               # Oversampling in each dimension\n                                       # (0 = automatic)\nINTERPOLATE            N               # Interpolate bad input pixels (Y/N)?\n                                       # (all or for each image)\n\nFSCALASTRO_TYPE        FIXED           # NONE,FIXED, or VARIABLE\nFSCALE_KEYWORD         FLXSCALE        # FITS keyword for the multiplicative\n                                       # factor applied to each input image\nFSCALE_DEFAULT         1.0             # Default FSCALE value if not in header\n\nGAIN_KEYWORD           GAIN            # FITS keyword for effect. gain (e-/ADU)\nGAIN_DEFAULT           0.0             # Default gain if no FITS keyword found\n\n')
                f.write('#--------------------------- Background subtraction ---------------------------\n\nSUBTRACT_BACK          N               # Subtraction sky background (Y/N)?\n                                       # (all or for each image)\n\nBACK_TYPE              AUTO            # AUTO or MANUAL\n                                       # (all or for each image)\nBACK_DEFAULT           0.0             # Default background value in MANUAL\n                                       # (all or for each image)\nBACK_SIZE              128             # Background mesh size (pixels)\n                                       # (all or for each image)\nBACK_FILTERSIZE        3               # Background map filter range (meshes)\n                                       # (all or for each image)\n\n')
                f.write('#------------------------------ Memory management -----------------------------\n\nVMEM_DIR               .               # Directory path for swap files\nVMEM_MAX               2047            # Maximum amount of virtual memory (MB)\nMEM_MAX                2048            # Maximum amount of usable RAM (MB)\nCOMBINE_BUFSIZE        1024            # Buffer size for combine (MB)\n\n')
                f.write('#------------------------------ Miscellaneous ---------------------------------\n\nDELETE_TMPFILES        Y               # Delete temporary resampled FITS files\n                                       # (Y/N)?\nCOPY_KEYWORDS          OBJECT          # List of FITS keywords to propagate\n                                       # from the input to the output headers\nWRITE_FILEINFO         Y               # Write information about each input\n                                       # file in the output image header?\nWRITE_XML              N               # Write XML file (Y/N)?\nXML_NAME               swarp.xml       # Filename for XML output\nVERBOSE_TYPE           QUIET           # QUIET,NORMAL or FULL\n\nNTHREADS               0               # Number of simultaneous threads for\n                                       # the SMP version of SWarp\n                                       # 0 = automatic \n')
                f.write('EOT\n')
                f.write('swarp ' + ' '.join(filename_list) + '\n\n')
                f.write('rm ' + os.path.join(output_dir, '_*'))
                f.close()
                
            filename = '{}.fits'.format(os.path.join(output_dir, '_'.join([output_name, band])))
            os.system('/bin/bash config_swarp.sh')
            
            if imgtype == 'mask':
                hdu = fits.open(filename)[0]
                mask = (hdu.data > 0).astype(float)
                mask_file = fits.PrimaryHDU( mask, hdu.header )
                mask_file.writeto(filename, overwrite = True)
                
            if logger is not None:
                logger.info(f'The stitched {imgtype} is saved as {filename}')
                
        elif method == 'reproject':
            from reproject import reproject_interp
            from reproject.mosaicking import reproject_and_coadd
            from mrf.utils import save_to_fits
            from astropy import wcs
            
            if imgtype == 'mask':
                combine_type = 'sum'
                match_background = "False"
            else:
                combine_type = 'mean'
                match_background = "True"
            
            target_name = config.target_name
            df_header = fits.getheader(f'./Images/{target_name}-df-{band}.fits')
            array, footprint = reproject_and_coadd(filename_list, df_header,
                                                shape_out = [imgsize, imgsize],
                                                reproject_function=reproject_interp,
                                                combine_function=combine_type, 
                                                match_background=match_background,
                                                background_reference=None)
            filename = '{}.fits'.format(os.path.join(output_dir, '_'.join([output_name, band])))
            
            hdu = save_to_fits(array, filename, wcs=wcs.WCS(df_header), overwrite=True)
            if logger is not None:
                logger.info(f'The stitched {imgtype} is saved as {filename}')