#!/usr/bin/python
"""maphot is a wrapper for easily running trippy on a bunch of images
of a single object on a single night.
Usage: (-h gives this line as well)
maphot.py -c <coofile> -f <fitsfile> -v False -. True -o False -r True -a 07
Defaults are:
inputFile = 'a100.fits'  # Change with '-f <filename>' flag
coofile = 'coords.in'  # Change with '-c <coofile>' flag
verbose = False  # Change with '-v True' or '--verbose True'
centroid = True  # Change with '-. False' or  --centroid False'
overrideSEx = False  # Change with '-o True' or '--override True'
remove = True  # Change with '-r False' or '--remove False'
aprad = 0.7  # Change with '-a 1.5' or '--aprad 1.5'
coordsfile is a file that contains:
x1 y1 MJD1
x2 y2 MJD2
ie. the position of the TNO at two times, and those times in MJD format.
The script then reads the MJD keyword from the input files and extrapolates
in order to predict the location in that image.
"""

from __future__ import print_function, division
import os
import getopt
import sys
from six.moves import input, zip
import numpy as np
import pylab as pyl
import astropy.io.fits as pyf
from astropy.visualization import interval
from astropy.io.votable import parse_single_table
from astropy.table import Column
#from astropy.io import fits
import mp_ephem
from trippy import psf, pill, psfStarChooser, scamp, MCMCfit
from stsci import numdisplay
import requests
import best
__author__ = ('Mike Alexandersen (@mikea1985, github: mikea1985, '
              'mike.alexandersen@alumni.ubc.ca)')


def queryPanSTARRS(ra_deg, dec_deg, rad_deg=0.1, mindet=1, maxsources=10000,
                   server=('https://archive.stsci.edu/panstarrs/search.php'),
                   catalog_filename='panstarrs.xml'):
  '''
  This function is inspired by Michael Mommert's wordpress post about querying
  PanSTARRS1 from Python:
  https://michaelmommert.wordpress.com/2017/02/13/
  accessing-the-gaia-and-pan-starrs-catalogs-using-python/
  Query Pan-STARRS DR1 @ MAST
  parameters: ra_deg, dec_deg, rad_deg: RA, Dec, field radius in degrees
              mindet: minimum number of detection (optional)
              maxsources: maximum number of sources
              server: servername
              catalog_filename: the filename to save the catalog query to.
  returns: astropy.table object
  '''
  r = requests.get(server, params={'RA': ra_deg, 'DEC': dec_deg,
                                   'SR': rad_deg, 'max_records': maxsources,
                                   'outputformat': 'VOTable',
                                   'ndetections': ('>%d' % mindet)})
  # write query data into local file
  outf = open(catalog_filename, 'w')
  outf.write(r.text)
  outf.close()
  return


def readPanSTARRS(catalog_filename='panstarrs.xml', rMin=0, gMin=0,
                  PSF_Kron=0.5):
  '''
  Read a PanSTARRS catalog from an xml file.
  Only include objects that have both g and r-band magnitudes.
  '''
  # Read an xml file and parse it into an astropy.table object.
  PS1CatDataFull = parse_single_table(catalog_filename)
  PS1All = PS1CatDataFull.to_table(use_names_over_ids=True)
  PS1Cat = PS1All[(PS1All['rMeanPSFMag'] > rMin)
                  & (PS1All['gMeanPSFMag'] > gMin)
                  & (PS1All['rMeanPSFMag'] - PS1All['rMeanKronMag'] < PSF_Kron)
                  & (PS1All['gMeanPSFMag']
                     - PS1All['gMeanKronMag'] < PSF_Kron)]
  return PS1Cat


def PS1_vs_SEx(PS1Cat, SExCat, maxDist=1):
  '''
  Match sources in the PanSTARRS and Source Extractor catalogs.
  Return only the overlapping catalog, with all columns from both catalogs.
  With this, we probably don't need to use catalogTrim. Maybe? Let's see.
  '''
  SExArgs = []
  PS1Args = []
  for ii, RADeci in enumerate(PS1Cat['raMean', 'decMean']):
    distance = ((SExCat['X_WORLD'] - RADeci[0]) ** 2 +
                (SExCat['Y_WORLD'] - RADeci[1]) ** 2) ** 0.5
    dminSExArg = np.argsort(distance)[0]
    if distance[dminSExArg] < maxDist / 3600.:
      SExArgs.append(dminSExArg)
      PS1Args.append(ii)
  PS1SExCatalog = PS1Cat[PS1Args]
  PS1SExCatalog.add_columns([Column(SExCat[key][SExArgs], key)
                             for key in SExCat.keys()])
  return PS1SExCatalog


def trimCatalog(cat, somedata, dcut, mcut, snrcut, shapecut):
  """trimCatalog trims the SExtractor catalogue of non-roundish things,
  really bright things and things that are near other things.
  cat = the full catalogue from SExtractor.
  dcut = the minimum acceptable distance between sources.
  mcut = maximum count (in image counts); removes really bright sources.
  snrcut = minimum Signal-to-Noise to keep; remove faint objects.
  shapecut = maximum long-axis/short-axis shape value; remove galaxies.
  """
  good = []
  for ii in range(len(cat['XWIN_IMAGE'])):
    try:
      a = int(cat['XWIN_IMAGE'][ii])
      b = int(cat['YWIN_IMAGE'][ii])
      m = np.max(somedata[b - 4:b + 5, a - 4:a + 5])
    except:
      pass
    xi = cat['XWIN_IMAGE'][ii]
    yi = cat['YWIN_IMAGE'][ii]
    distance = np.sort(((cat['XWIN_IMAGE'] - xi) ** 2 +
                        (cat['YWIN_IMAGE'] - yi) ** 2) ** 0.5)
    d = distance[1]
    snrs = cat['FLUX_AUTO'][ii] / cat['FLUXERR_AUTO'][ii]
    shape = cat['AWIN_IMAGE'][ii] / cat['BWIN_IMAGE'][ii]
    if (cat['FLAGS'][ii] == 0
       and d > dcut
       and m < mcut
       and snrs > snrcut
       and shape < shapecut
       and xi > dcut + 1 and xi < naxis1 - dcut - 1
       and yi > dcut + 1 and yi < naxis2 - dcut - 1):
      good.append(ii)
  good = np.array(good)
  outcat = {}
  for ii in cat:
    outcat[ii] = cat[ii][good]
  return outcat


def getObservations(mpc_lines):
  '''Parces MPC lines and generates an mp_ephem observation.'''
  observationList = []
  for _, mpc_line in enumerate(mpc_lines):
    date = mpc_line[15:31]
    ra = mpc_line[32:44]
    dec = mpc_line[44:57]
    obsCode = mpc_line[-4:-1]
    observationList.append(mp_ephem.ephem.Observation(ra=ra, dec=dec,
                                                      date=date,
                                                      observatory_code=obsCode)
                           )
  return observationList


def coordRateAngle(orbit, MJDate, WCS, obs_code=568):
  '''Given an orbit and a date (MJDate),
  calculates the rate and angle of motion
  as seen from a given observatory (default is 568 - Mauna Kea, Hawai'i).'''
  orbit.predict(MJDate + 2400000.5, obs_code=obs_code)
  ra0, dec0 = orbit.coordinate.ra.degree, orbit.coordinate.dec.degree
  orbit.predict(MJDate + 2400000.5 + 1. / 24.0, obs_code=obs_code)
  ra1, dec1 = orbit.coordinate.ra.degree, orbit.coordinate.dec.degree
  #rate_deg = ((np.cos(dec0 * np.pi / 180) * (ra1 - ra0)) ** 2
  #            + (dec1 - dec0) ** 2) ** 0.5  # degrees per hour
  coords = WCS.wcs_world2pix(np.array([[ra0, dec0], [ra1, dec1]]), 1)
  rate_pix = ((coords[1, 0] - coords[0, 0]) ** 2
              + (coords[1, 1] - coords[0, 1]) ** 2) ** 0.5  # pix per hour
  angle_pix = (np.arctan2(coords[1, 1] - coords[0, 1],
                          coords[1, 0] - coords[0, 0]) * 180. / np.pi) % 180
  return coords[0, :], rate_pix, angle_pix


def writeSExParFiles(imageFileName, params):
  '''
  This writes a Source Extractor parameter file.
  '''
  sexFile = imageFileName.replace('.fits', '.sex')
  os.system('rm {}'.format(sexFile))
  os.system('rm def.param')
  os.system('rm default.conv')
  scamp.makeParFiles.writeSex(sexFile,
                              minArea=params[0], threshold=params[1],
                              zpt=params[2], aperture=params[3],
                              kron_factor=params[4], min_radius=params[5],
                              catalogType='FITS_LDAC', saturate=110000)
  scamp.makeParFiles.writeConv()
  scamp.makeParFiles.writeParam('def.param', numAps=1)


def runSExtractor(imageFileName, SExParams):
  '''
  Run Source Extractor. Provide a useful error if it fails.
  '''
  SExtractorFile = imageFileName.replace('.fits', '.sex')
  catalogFile = imageFileName.replace('.fits', '.cat')
  writeSExParFiles(imageFileName, SExParams)
  try:
    scamp.runSex(SExtractorFile, imageFileName,
                 options={'CATALOG_NAME': catalogFile})
    fullcatalog = scamp.getCatalog(catalogFile, paramFile='def.param')
  except IOError as error:
    raise IOError('\n{}\nYou have almost certainly forgotten '.format(error) +
                  'to activate Ureka or AstroConda!')
  return fullcatalog


def getSExCatalog(imageFileName, SExParams, verb=True):
  '''Checks whether a catalog file already exists.
  If it does, it is read in. If not, it runs Source Extractor to create it.
  '''
  catalogFile = imageFileName.replace('.fits', '.cat')
  try:
    fullcatalog = scamp.getCatalog(catalogFile, paramFile='def.param')
  except IOError:
    fullcatalog = runSExtractor(imageFileName, SExParams)
  except UnboundLocalError:
    print("\nData error occurred!\n")
    raise
  ncat = len(fullcatalog['XWIN_IMAGE'])
  print("\n" + str(ncat) + " catalog stars\n" if verb else "")
  return fullcatalog


'''
def getCatalogue(file_start):
  """getCatalog checks whether a catalog file already exists.
  If it does, it is read in. If not, it runs SExtractor to create it.
  """
  try:
    fullcatalog = scamp.getCatalog(file_start + '_fits.cat',
                                   paramFile='def.param')
  except IOError:
    try:
      scamp.makeParFiles.writeSex(file_start + '_fits.sex', minArea=3.0,
                                  #threshold=5.0, zpt=26.0, aperture=20.,
                                  threshold=5.0, zpt=MAGZERO, aperture=20.,
                                  min_radius=2.0, catalogType='FITS_LDAC',
                                  saturate=55000)
      scamp.makeParFiles.writeConv()
      scamp.makeParFiles.writeParam(numAps=1)
      scamp.makeParFiles.writeSex(file_start + '_ascii.sex', minArea=3.0,
                                  #threshold=5.0, zpt=26.0, aperture=20.,
                                  threshold=5.0, zpt=MAGZERO, aperture=20.,
                                  min_radius=2.0, catalogType='ASCII',
                                  saturate=55000)
      scamp.makeParFiles.writeConv()
      scamp.makeParFiles.writeParam(numAps=1)
      scamp.runSex(file_start + '_fits.sex', file_start + '.fits',
                   options={'CATALOG_NAME': file_start + '_fits.cat'})
      scamp.runSex(file_start + '_ascii.sex', file_start + '.fits',
                   options={'CATALOG_NAME': file_start + '_ascii.cat'})
      fullcatalog = scamp.getCatalog(file_start + '_fits.cat',
                                     paramFile='def.param')
    except IOError as error:
      print("IOError: ", error)
      print("You have almost certainly forgotten to activate Ureka!")
      raise
  except UnboundLocalError:
    print("\nData error occurred!\n")
    raise
  ncat = len(fullcatalog['XWIN_IMAGE'])
  print("\n", ncat, " catalog stars\n")
  outfile.write("\n{} catalog stars\n".format(ncat))
  return fullcatalog
'''

def runMCMCCentroid(centPSF, centData, centxt, centyt, centm,
                    centbg, centdtransx, centdtransy):
  """runMCMCCentroid runs an MCMC centroiding, fitting the TSF to the data.
  Returns the fitted centoid co-ordinates.
  """
  print("Should I be doing this?")
  print("MCMC-fitting TSF to the moving object")
  centfitter = MCMCfit.MCMCfitter(centPSF, centData)
  centfitter.fitWithModelPSF(centdtransx + centxt - int(centxt),
                             centdtransy + centyt - int(centyt),
                             m_in=centm / repfact ** 2.,
                             fitWidth=10, nWalkers=10,
                             nBurn=20, nStep=20, bg=centbg, useLinePSF=True,
                             verbose=True, useErrorMap=False)
  (centfitPars, centfitRange) = centfitter.fitResults(0.67)
# Reverse the above coordinate transformation:
  xcentroid, ycentroid = centfitPars[0:2] \
                         - [dtransx, dtransy] \
                         + [int(centxt), int(centyt)]  # noqa
  return xcentroid, ycentroid, centfitPars, centfitRange


def getArguments(sysargv):
  """Get arguments given when this is called from a command line"""
  AinputFile = 'a100.fits'  # Change with '-f <filename>' flag
  Acoordsfile = 'coords.in'  # Change with '-c <coordsfile>' flag
  Averbose = False  # Change with '-v True' or '--verbose True'
  Acentroid = False  # Change with '-. False' or  --centroid False'
  AoverrideSEx = False  # Change with '-o True' or '--override True'
  Aremove = False  # Change with '-r False' or '--remove False'
  Aaprad = -42.
  Arepfact = 10
  Apxscale = 1.0
  AroundAperRad = 1.4
  try:
    options, dummy = getopt.getopt(sysargv[1:], "f:c:v:.:o:r:a:h:",
                                   ["ifile=", "coords=", "verbose=",
                                    "centroid=", "overrideSEx=",
                                    "remove=", "aprad="])
    for opt, arg in options:
      if (opt in ("-v", "-verbose", "-.", "--centroid", "-o", "--overrideSEx",
                  "-r", "--remove")):
        if arg == '0' or arg == 'False':
          arg = False
        elif arg == '1' or arg == 'True':
          arg = True
        else:
          print(opt, arg, np.array([arg]).dtype)
          raise TypeError("-v -. -o -r flags must be followed by " +
                          "0/False/1/True")
      if opt == '-h':
        print(useage)
      elif opt in ('-f', '--ifile'):
        AinputFile = arg
      elif opt in ('-c', '--coords'):
        Acoordsfile = arg
      elif opt in ('-v', '--verbose'):
        Averbose = arg
      elif opt in ('-.', '--centroid'):
        Acentroid = arg
      elif opt in ('-o', '--overrideSEx'):
        AoverrideSEx = arg
      elif opt in ('-r', '--remove'):
        Aremove = arg
      elif opt in ('-a', '--aprad'):
        Aaprad = float(arg)
  except TypeError as error:
    print(error)
    sys.exit()
  except getopt.GetoptError as error:
    print(" Input ERROR! ")
    print(useage)
    sys.exit(2)
  return (AinputFile, Acoordsfile, Averbose, Acentroid,
          AoverrideSEx, Aremove, Aaprad, Arepfact, Apxscale, AroundAperRad)


def findTNO(xzero, yzero):
  """Finds the nearest catalogue entry to the estimated location."""
  dist = ((fullcat['XWIN_IMAGE'] - xzero) ** 2
          + (fullcat['YWIN_IMAGE'] - y0) ** 2) ** 0.5
  args = np.argsort(dist)
  print("\n x0, y0 = ", xzero, yzero, "\n")
  outfile.write("\nx0, y0 = {}, {}\n".format(xzero, yzero))
  xtno = fullcat['XWIN_IMAGE'][args][0]
  ytno = fullcat['YWIN_IMAGE'][args][0]
  if (xtno - xzero) ** 2 + (ytno - yzero) ** 2 > 36:
    print("\n   WARNING! Object not found at", xzero, yzero, "\n")
    outfile.write("\n   WARNING! Object not found at {}, {}\n".format(xzero,
                                                                      yzero))
    xtno, ytno = xzero, yzero
  return xtno, ytno


###############################################################################

useage = 'maphot -c <coordsfile> -f <fitsfile> -v False '\
         + '-. False -o False -r False -a 0.7'
(inputFile, coordsfile, verbose, centroid, overrideSEx, remove,
 aprad, repfact, pxscale, roundAperRad) = getArguments(sys.argv)

print("ifile =", inputFile, ", coords =", coordsfile, ", verbose =", verbose,
      ", centroid =", centroid, ", overrideSEx =", overrideSEx,
      ", remove =", remove, ", aprad =", aprad)
if verbose:
  print(np.array([centroid]).dtype, np.array([remove]).dtype)
  if centroid or remove:
    print("Will run MCMC centroiding")

xin, yin, mjdin = np.genfromtxt(coordsfile, usecols=(0, 1, 2), unpack=True)
rate = (((xin[1] - xin[0]) ** 2 + (yin[1] - yin[0]) ** 2) ** 0.5
        / ((mjdin[1] - mjdin[0]) * 24.) * pxscale)  # "/hr
angle = (np.arctan2(yin[1] - yin[0], xin[1] - xin[0])
         * 180 / np.pi)  # deg c-clockw from +x axis

inputName = inputFile[:-5]
outfile = open(inputName + '.trippy', 'w')
with pyf.open(inputFile) as han:
  data = han[0].data
  header = han[0].header
  EXPTIME = header['EXPTIME']
  try:
    MAGZERO = header['MAGZERO']
  except:
    MAGZERO = 26.0
  try:
    MJD = header['MJD']
  except:
    MJD = header['MJDATE']
  try:
    gain = header['GAINEFF']
  except:
    gain = header['GAIN']
  naxis1 = header['NAXIS1']
  naxis2 = header['NAXIS2']
x0 = (xin[0] + rate / pxscale * np.cos(angle * np.pi / 180)
      * (MJD - mjdin[0]) * 24)  # Aprx TNO location
y0 = (yin[0] + rate / pxscale * np.sin(angle * np.pi / 180)
      * (MJD - mjdin[0]) * 24)  # -||-

print("\nWorking on ", inputFile, "\n")
outfile.write("\nWorking on {}.\n".format(inputFile))
print("\nMJD = ", MJD, "\n")
outfile.write("\nMJD = {}\n".format(MJD))

fullcat = getSExCatalog(inputName, SEx_params)

if overrideSEx:
  xt, yt = x0, y0
else:
  xt, yt = findTNO(x0, y0)

print("xt, yt = ", xt, yt, "\n")
outfile.write("xt, yt = {}, {}\n".format(xt, yt))

"""  # Commenting this out until replacement has been tested thoroughly.
ncat_psf = 0
ncat_phot = 0
i = 0
while ncat_psf < 150:
  catalog_psf = trimCatalog(fullcat, data, 30, 70000, 50 - i, 1.15 + i / 100.)
  catalog_phot = trimCatalog(fullcat, data, 30, 70000, 30 - i, 1.15 + i / 100.)
  ncat_psf = len(catalog_psf['XWIN_IMAGE'])
  ncat_phot = len(catalog_phot['XWIN_IMAGE'])
  i += 1
print("\n", ncat_psf, ncat_phot,
      " trimmed catalog stars ({} iterations)\n".format(i - 1))
outfile.write("\n{}, {} trimmed catalog stars".format(ncat_psf, ncat_phot) +
              " ({} iterations)\n".format(i - 1))
"""
try:
  bestcat = best.unpickleCatalogue('best.cat')
  print('Success!')
except IOError:
  print('Uh oh!')
  best.best(['a' + str(i) for i in range(100, 124)], repfact)
  bestcat = best.unpickleCatalogue('best.cat')
catalog_psf = best.findSharedCatalogue([fullcat, bestcat], 0)
catalog_phot = catalog_psf

try:
  goodPSF = psf.modelPSF(restore=inputName + '_psf.fits')
  fwhm = goodPSF.FWHM()  # I think this is FWHM with lookuptable included
  print("\nPSF restored from file.\n")
  outfile.write("\nPSF restored from file\n")
#  goodPSF.fitted=False
  print("fwhm = ", fwhm)
  outfile.write("\nfwhm = {}\n".format(fwhm))
except IOError:
  print("Could not restore PSF (Normal unless previously saved)\n")
  print("Making new one.\n")
  outfile.write("\nDid not restore PSF from file\n")
  starChooser = psfStarChooser.starChooser(data, catalog_psf['XWIN_IMAGE'],
                                           catalog_psf['YWIN_IMAGE'],
                                           catalog_psf['FLUX_AUTO'],
                                           catalog_psf['FLUXERR_AUTO'])
  (goodFits, goodMeds, goodSTDs
   ) = starChooser(30, 100,  # (box size, min SNR)
                   initAlpha=3., initBeta=3.,
                   repFact=repfact,
                   includeCheesySaturationCut=False,
                   verbose=False)
  print("\ngoodFits = ", goodFits, "\n")
  print("\ngoodMeds = ", goodMeds, "\n")
  print("\ngoodSTDs = ", goodSTDs, "\n")
  outfile.write("\ngoodFits={}".format(goodFits))
  outfile.write("\ngoodMeds={}".format(goodMeds))
  outfile.write("\ngoodSTDs={}".format(goodSTDs))
  goodPSF = psf.modelPSF(np.arange(61), np.arange(61), alpha=goodMeds[2],
                         beta=goodMeds[3], repFact=repfact)
  fwhm = goodPSF.FWHM()  # this is the pure moffat FWHM
  print("fwhm = ", fwhm)
  outfile.write("\n fwhm = {}\n".format(fwhm))
  goodPSF.genLookupTable(data, goodFits[:, 4], goodFits[:, 5], verbose=False)
  goodPSF.genPSF()
  fwhm = goodPSF.FWHM()  # this is the FWHM with lookuptable included
  print("fwhm = ", fwhm)
  outfile.write("\n fwhm = {}\n".format(fwhm))
except UnboundLocalError:
  print("Data error occurred!")
  outfile.write("\nData error occured!\n")
  raise

goodPSF.line(rate, angle, EXPTIME / 3600., pixScale=pxscale,
             useLookupTable=True)
goodPSF.computeRoundAperCorrFromPSF(psf.extent(0.8 * fwhm, 4 * fwhm, 100),
                                    display=False,
                                    displayAperture=False,
                                    useLookupTable=True)
roundAperCorr = goodPSF.roundAperCorr(roundAperRad * fwhm)
goodPSF.computeLineAperCorrFromTSF(psf.extent(0.1 * fwhm, 4 * fwhm, 100),
                                   l=(EXPTIME / 3600.) * rate / pxscale,
                                   a=angle, display=False,
                                   displayAperture=False)
goodPSF.psfStore(inputName + '_psf.fits')

# Do photometry for the trimmed catalog stars.
# This will be used to find a set of non-variable stars, in order to
# subtract fluctuations due to seeing, airmass, etc.
bgstars = []
print('\nPhotometry of catalog stars\n')
outfile.write("\n# Photometry of catalog stars\n")
outfile.write("\n#   x       y   magnitude  dmagnitude")
for xcat, ycat in np.array(list(zip(catalog_phot['XWIN_IMAGE'],
                                    catalog_phot['YWIN_IMAGE']))):
  phot = pill.pillPhot(data, repFact=repfact)
  phot(xcat, ycat, radius=fwhm * roundAperRad, l=0.0, a=0.0, exptime=EXPTIME,
       #zpt=26.0, skyRadius=4 * fwhm, width=30.,
       zpt=MAGZERO, skyRadius=4 * fwhm, width=30.,
       enableBGSelection=verbose, display=verbose, backupMode="smart",
       trimBGHighPix=3., zscale=False)
  phot.SNR(gain=gain, useBGstd=True)
  print(xcat, ycat, phot.magnitude - roundAperCorr, phot.dmagnitude)
  outfile.write("\n{0:13.8f} {1:13.8f} {2:13.10f} {3:13.10f}".format(
                xcat, ycat, phot.magnitude - roundAperCorr, phot.dmagnitude))
  bgstars.append(phot.bg)

bgmedian = np.median(bgstars)
Data = (data[np.max([0, int(yt) - 200]):np.min([naxis2 - 1, int(yt) + 200]),
             np.max([0, int(xt) - 200]):np.min([naxis1 - 1, int(xt) + 200])]
        - bgmedian)
dtransy = int(yt) - np.max([0, int(yt) - 200]) - 1
dtransx = int(xt) - np.max([0, int(xt) - 200]) - 1
Zoom = (data[np.max([0, int(yt) - 15]):np.min([naxis2 - 1, int(yt) + 15]),
             np.max([0, int(xt) - 15]):np.min([naxis1 - 1, int(xt) + 15])]
        - bgmedian)
zy = int(yt) - np.max([0, int(yt) - 15]) - 1
zx = int(xt) - np.max([0, int(xt) - 15]) - 1
m_obj = np.max(data[np.max([0, int(yt) - 5]):
                    np.min([naxis2 - 1, int(yt) + 5]),
                    np.max([0, int(xt) - 5]):
                    np.min([naxis1 - 1, int(xt) + 5])])

'''
Use MCMC fitting to fit the TSF to the object, thus centroiding on it.
This is often NOT better than the SExtractor location, especially
when the object is only barely trailed or
when the sky has a gradient (near something bright).
This fit is also used to remove the object from the image, later.
fit takes time proportional to nWalkers*(2+nBurn+nStep).
'''
xt0, yt0 = xt, yt
while True:
  (z1, z2) = numdisplay.zscale.zscale(Zoom)
  normer = interval.ManualInterval(z1, z2)
  pyl.imshow(normer(Zoom), origin='lower')
  pyl.plot([zx + x0 - int(xt0)], [zy + y0 - int(yt0)], 'k*', ms=10)
  pyl.plot([zx + xt0 - int(xt0)], [zy + yt0 - int(yt0)], 'w+', ms=10, mew=2)
  if centroid or remove:
    print("Should I be doing this?")
    xcent, ycent, fitPars, fitRange = runMCMCCentroid(goodPSF, Data, x0, y0,
                                                      m_obj, bgmedian,
                                                      dtransx, dtransy)
    pyl.plot([zx + xcent - int(xt0)],
             [zy + ycent - int(yt0)], 'gx', ms=10, mew=2)
    print("Estimated    (black)  x,y = ", x0, y0)
    print("SExtractor   (white)  x,y = ", xt, yt)
    print("MCMCcentroid (green)  x,y = ", xcent, ycent)
    pyl.show()
    yn = input('Accept MCMC centroid (m or c), '
               + 'SExtractor centroid (S), or estimate (e)? ')
    if ('e' in yn) or ('E' in yn):  # if press e/E use estimate
      xt, yt = x0, y0
      break
    elif ('m' in yn) or ('M' in yn) or ('c' in yn) or ('C' in yn):  # centroid
      xt, yt = xcent, ycent
      break
    else:
      yn = 'S'  # else do nothing, use SExtractor co-ordinates.
      break
  else:  # if not previously centroided, check whether needed
    if (x0 == xt) & (y0 == yt):  # if TNO not seen in SExtractor, run centroid
      centroid = True
    else:  # else pick between estimate, SExtractor and recentroiding
      print("Estimated    (black)  x,y = ", x0, y0)
      print("SExtractor   (white)  x,y = ", xt, yt)
      pyl.show()
      yn = input('Accept '
                 + 'SExtractor centroid (S), or estimate (e), '
                 + ' or recentroid using MCMC (m or c)? ')
      if ('e' in yn) or ('E' in yn):  # if press e/E use estimate
        xt, yt = x0, y0
        break
      elif ('m' in yn) or ('M' in yn) or ('c' in yn) or ('C' in yn):  # cntroid
        centroid = True
      else:
        yn = 'S'  # else do nothing, use SExtractor co-ordinates.
        break


print('\nPhotometry of moving object\n')
outfile.write("\nPhotometry of moving object\n")
phot = pill.pillPhot(data, repFact=repfact)
# Make sure to use IRAF coordinates not numpy/sextractor coordinates!
apertures = np.arange(0.7, 1.6, 0.1)
linedmag = np.zeros(len(apertures))
for i, ap in enumerate(apertures):
  phot(xt, yt, radius=fwhm * ap, l=(EXPTIME / 3600.) * rate / pxscale,
       a=angle, skyRadius=4 * fwhm, width=6 * fwhm,
       #zpt=26.0, exptime=EXPTIME, enableBGSelection=False, display=False,
       zpt=MAGZERO, exptime=EXPTIME, enableBGSelection=False, display=False,
       backupMode="smart", trimBGHighPix=3., zscale=False)
  phot.SNR(gain=gain, verbose=False, useBGstd=True)
  linedmag[i] = phot.dmagnitude
bestap = apertures[np.argmin(linedmag)]
if aprad > 0:
  bestap = np.arange(aprad, aprad + 1)[0]  # stupid but wouldn't work otherwise
lineAperRad = bestap
print("\nBest aperture = ", bestap)
outfile.write("\nBest aperture = {}".format(bestap))
lineAperCorr = goodPSF.lineAperCorr(lineAperRad * fwhm)
print("\nlineAperCorr, roundAperCorr = ", lineAperCorr, roundAperCorr, "\n")
outfile.write("\nlineAperCorr,roundAperCorr={},{}".format(lineAperCorr,
                                                          roundAperCorr))

phot(xt, yt, radius=fwhm * lineAperRad, l=(EXPTIME / 3600.) * rate / pxscale,
     a=angle, skyRadius=4 * fwhm, width=6 * fwhm,
     #zpt=26.0, exptime=EXPTIME, enableBGSelection=True, display=True,
     zpt=MAGZERO, exptime=EXPTIME, enableBGSelection=True, display=True,
     backupMode="smart", trimBGHighPix=3., zscale=False)
phot.SNR(gain=gain, verbose=True, useBGstd=True)

# Get those values
print("phot.magnitude = ", phot.magnitude)
print("phot.dmagnitude = ", phot.dmagnitude)
print("phot.sourceFlux = ", phot.sourceFlux)
print("phot.snr = ", phot.snr)
print("phot.bg = ", phot.bg)
outfile.write("\nphot.magnitude={}".format(phot.magnitude))
outfile.write("\nphot.dmagnitude={}".format(phot.dmagnitude))
outfile.write("\nphot.sourceFlux={}".format(phot.sourceFlux))
outfile.write("\nphot.snr={}".format(phot.snr))
outfile.write("\nphot.bg={}".format(phot.bg))

print("\nFINAL RESULT!")
print("\n#{0:12} {1:13} {2:13} {3:13} {4:13}".format(
      '   x ', '    y ', ' magnitude ', '  dmagnitude ', ' magzero '))
print("{0:13.8f} {1:13.8f} {2:13.10f} {3:13.10f} {4:13.10f}\n".format(
      xt, yt, phot.magnitude - lineAperCorr, phot.dmagnitude, MAGZERO))
outfile.write("\nFINAL RESULT!")
outfile.write("\n#{0:12} {1:13} {2:13} {3:13} {4:13}\n".format(
              '   x ', '    y ', ' magnitude ', '  dmagnitude ', ' magzero '))
outfile.write("{0:13.8f} {1:13.8f} {2:13.10f} {3:13.10f} {4:13.10f}\n".format(
              xt, yt, phot.magnitude - lineAperCorr, phot.dmagnitude, MAGZERO))

# You could stop here.
# However, to confirm that things are working well,
# let's generate the trailed PSF and subtract the object out of the image.
if centroid and remove and (('e' in yn) or ('E' in yn) or
                            ('s' in yn) or ('S' in yn)):
  Data = (data[np.max([0, int(yt) - 200]):np.min([naxis2 - 1, int(yt) + 200]),
               np.max([0, int(xt) - 200]):np.min([naxis1 - 1, int(xt) + 200])]
          - phot.bg)
  dtransy = int(yt) - np.max([0, int(yt) - 200]) - 1
  dtransx = int(xt) - np.max([0, int(xt) - 200]) - 1
  m_obj = np.max(data[np.max([0, int(yt) - 5]):
                      np.min([naxis2 - 1, int(yt) + 5]),
                      np.max([0, int(xt) - 5]):
                      np.min([naxis1 - 1, int(xt) + 5])])
  print("Should I be doing this?")
  fitter = MCMCfit.MCMCfitter(goodPSF, Data)
  fitter.fitWithModelPSF(dtransx + xt - int(xt), dtransy + yt - int(yt),
                         m_in=m_obj / repfact ** 2., fitWidth=2, nWalkers=10,
                         nBurn=10, nStep=10, bg=phot.bg, useLinePSF=True,
                         verbose=True, useErrorMap=False)
  (fitPars, fitRange) = fitter.fitResults(0.67)

if centroid or remove:
  print("\nfitPars = ", fitPars, "\n")
  print("\nfitRange = ", fitRange, "\n")
  outfile.write("\nfitPars={}".format(fitPars))
  outfile.write("\nfitRange={}".format(fitRange))
  removed = goodPSF.remove(fitPars[0], fitPars[1], fitPars[2],
                           Data, useLinePSF=True)
  (z1, z2) = numdisplay.zscale.zscale(removed)
  normer = interval.ManualInterval(z1, z2)
  modelImage = goodPSF.plant(fitPars[0], fitPars[1], fitPars[2], Data,
                             addNoise=False, useLinePSF=True, returnModel=True)
  pyl.imshow(normer(goodPSF.lookupTable), origin='lower')
  pyl.show()
  #pyl.imshow(normer(modelImage), origin='lower')
  #pyl.show()
  #pyl.imshow(normer(Data), origin='lower')
  #pyl.show()
  #pyl.imshow(normer(removed), origin='lower')
  #pyl.show()
  hdu = pyf.PrimaryHDU(modelImage, header=han[0].header)
  list = pyf.HDUList([hdu])
  list.writeto(inputName + '_modelImage.fits', overwrite=True)
  hdu = pyf.PrimaryHDU(removed, header=han[0].header)
  list = pyf.HDUList([hdu])
  list.writeto(inputName + '_removed.fits', overwrite=True)
else:
  (z1, z2) = numdisplay.zscale.zscale(Data)
  normer = interval.ManualInterval(z1, z2)
  pyl.imshow(normer(goodPSF.lookupTable), origin='lower')
  pyl.show()
  #pyl.imshow(normer(Data), origin='lower')
  #pyl.show()

hdu = pyf.PrimaryHDU(goodPSF.lookupTable, header=han[0].header)
list = pyf.HDUList([hdu])
list.writeto(inputName + '_lookupTable.fits', overwrite=True)
hdu = pyf.PrimaryHDU(Data, header=han[0].header)
list = pyf.HDUList([hdu])
list.writeto(inputName + '_Data.fits', overwrite=True)

outfile.close()
