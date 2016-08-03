require('ng-file-upload');

angular.module('app.preferences').config(preferencesConfig);

preferencesConfig.$inject = ['$stateProvider'];
function preferencesConfig($stateProvider) {
    $stateProvider.state('base.preferences.user.profile', {
        parent: 'base.preferences.user',
        url: '/profile',
        views: {
            '@base.preferences': {
                templateUrl: 'preferences/user/controllers/user_profile.html',
                controller: PreferencesUserProfileController,
                controllerAs: 'vm',
            },
        },
        resolve: {
            userProfile: ['User', function(User) {
                return User.me().$promise;
            }],
        },
    });
}

angular.module('app.preferences').controller('PreferencesUserProfileController', PreferencesUserProfileController);

PreferencesUserProfileController.$inject = ['$window', 'HLForms', 'HLUtils', 'Upload', 'User', 'userProfile'];
function PreferencesUserProfileController($window, HLForms, HLUtils, Upload, User, userProfile) {
    var vm = this;

    vm.user = userProfile;

    vm.saveUser = saveUser;

    //////

    function saveUser(form) {
        var formName = '[name="userForm"]';

        // Manually set the fields because Upload.upload doesn't
        // seem to handle Angular resources very well.
        var data = {
            'first_name': vm.user.first_name,
            'last_name': vm.user.last_name,
            'position': vm.user.position,
            'phone_number': vm.user.phone_number,
            'twitter': vm.user.twitter,
            'linkedin': vm.user.linkedin,
        };

        if (vm.user.picture) {
            data.picture = vm.user.picture;
        }

        HLUtils.blockUI(formName, true);

        // Clear all errors of the form (in case of new errors).
        angular.forEach(form, function(value, key) {
            if (typeof value === 'object' && value.hasOwnProperty('$modelValue')) {
                form[key].$error = {};
                form[key].$setValidity(key, true);
            }
        });

        Upload.upload({
            url: 'api/users/user/me/',
            method: 'PUT',
            data: data,
        }).then(function() {
            toastr.success('I\'ve updated your profile!', 'Done');
            // Regular state reload isn't enough here, because the picture isn't reloaded.
            // So do a full refresh to show the updated picture.
            $window.location.reload();
        }, function(response) {
            HLUtils.unblockUI(formName);
            _handleBadResponse(response, form);
        });
    }

    function _handleBadResponse(response, form) {
        HLForms.setErrors(form, response.data);

        toastr.error('Uh oh, there seems to be a problem', 'Oops!');
    }
}
